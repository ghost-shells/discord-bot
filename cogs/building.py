# building.py – full file with payment verification and log channel

import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from cogs.config import admin_only, get_guild_config
from cogs.tickets import TicketView, _close_ticket
import asyncio
import aiohttp
import os
import logging

# ── Environment ──────────────────────────────────────────────────────────
DONUTSMP_API_URL = os.getenv("DONUTSMP_API_URL", "https://api.donutsmp.com")
DONUTSMP_API_KEY = os.getenv("DONUTSMP_API_KEY")

logger = logging.getLogger(__name__)

# ── Permission Checks ──────────────────────────────────────────────────
def is_builder(interaction: discord.Interaction) -> bool:
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    user_role_ids = {r.id for r in interaction.user.roles}
    raw = cfg.get("BUILDER_ROLE_ID")
    if raw and int(raw) in user_role_ids:
        return True
    return interaction.user.guild_permissions.administrator

def has_cmd_perm(interaction: discord.Interaction, command_name: str) -> bool:
    """Checks if the user has permission based on the dashboard config."""
    if interaction.user.guild_permissions.administrator:
        return True
    db = interaction.client.db
    doc = db["command_perms"].find_one({"guild_id": interaction.guild.id, "command_name": command_name})
    if not doc or not doc.get("roles"):
        return False
    allowed_roles = doc["roles"]
    for role in interaction.user.roles:
        if role.name in allowed_roles:
            return True
    return False

# ── API Payment Check ──────────────────────────────────────────────────
async def get_player_balance(ign: str) -> float | None:
    """
    Fetch the current in-game balance for a player from the DonutSMP API.
    Endpoint: GET /v1/stats/{ign}
    The API returns text/plain (not JSON), so we read raw text and parse it.
    Returns the balance as a float, or None on error.
    """
    url = f"{DONUTSMP_API_URL}/v1/stats/{ign}"
    headers = {}
    if DONUTSMP_API_KEY:
        headers["Authorization"] = f"Bearer {DONUTSMP_API_KEY}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                raw = await resp.text()

                if resp.status != 200:
                    logger.error(
                        f"DonutSMP API error for {ign}: {resp.status} - {raw}"
                    )
                    return None

                # Try JSON first (in case the API ever changes content-type)
                import json as _json

                try:
                    data = _json.loads(raw)
                    result = data.get("result", data)  # fall back to data itself if no "result" key
                    balance = (
                        result.get("money")
                        or result.get("balance")
                        or result.get("coins")
                    )

                    if balance is not None:
                        return float(balance)

                except (_json.JSONDecodeError, AttributeError):
                    pass

                # Plain-text format: "key: value\nkey: value\n..."
                # e.g. "money: 1234567.89\nkills: 5\n..."
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue

                    for key in ("money", "balance", "coins"):
                        if line.lower().startswith(key):
                            # strip the key and any separators (: = space)
                            value_part = line[len(key):].lstrip(":= \t")

                            # strip commas and whitespace
                            value_part = value_part.replace(",", "").strip()

                            try:
                                return float(value_part)
                            except ValueError:
                                pass

                logger.error(
                    f"DonutSMP API: could not find money in response for {ign}. Raw: {raw!r}"
                )
                return None

        except Exception as e:
            logger.error(f"DonutSMP API request failed for {ign}: {e}")
            return None



def parse_price(price_str: str) -> float | None:
    """
    Convert price strings like '500k', '1.5m', '10000' to a float.
    Returns None if the string can't be parsed.
    """
    price_str = price_str.strip().lower().replace(",", "")
    try:
        if price_str.endswith("k"):
            return float(price_str[:-1]) * 1_000
        elif price_str.endswith("m"):
            return float(price_str[:-1]) * 1_000_000
        elif price_str.endswith("b"):
            return float(price_str[:-1]) * 1_000_000_000
        else:
            return float(price_str)
    except (ValueError, AttributeError):
        return None

# ── Background Payment Monitor ────────────────────────────────────────
async def monitor_payment(order_id: str, db, guild_id: int, buyer_ign: str, receiver_ign: str,
                          amount: str, buyer_id: int, build: dict, modal_data: dict, bot,
                          ticket_channel_id: int | None = None):
    """
    Poll the DonutSMP /v1/stats/{ign} endpoint every 10 seconds for up to 30 minutes.
    Payment is confirmed when the receiver's balance increases by >= the order price.

    ticket_channel_id (optional):
        When set, the ticket channel already exists (Custom Build path — opened immediately
        when the order was placed, price set later via /build money).
        On confirmation: unlock the buyer in the existing channel and post to builder orders.
        On expiry:       close the existing channel with a transcript.
        When None (regular build): create the ticket channel on confirmation as before.
    """
    start_time = datetime.now(timezone.utc)
    timeout = timedelta(minutes=30)
    check_interval = 10

    # Parse the expected payment amount once
    expected_amount = parse_price(amount)
    if expected_amount is None:
        logger.info(
            f"monitor_payment: price '{amount}' is not numeric for order {order_id}. "
            f"Cannot auto-monitor — caller should have validated price first."
        )
        db["building_orders"].update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {"payment_status": "error", "payment_error": f"Non-numeric price passed to monitor: {amount}"}}
        )
        return

    # Snapshot the receiver's balance before we start watching
    baseline_balance = await get_player_balance(receiver_ign)
    if baseline_balance is None:
        logger.error(f"monitor_payment: could not fetch baseline balance for {receiver_ign}. Aborting monitor.")
        db["building_orders"].update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {"payment_status": "error", "payment_error": "Could not reach DonutSMP API"}}
        )
        return

    logger.info(
        f"monitor_payment: order={order_id} receiver={receiver_ign} "
        f"baseline={baseline_balance} expected={expected_amount} "
        f"existing_channel={ticket_channel_id}"
    )

    while (datetime.now(timezone.utc) - start_time) < timeout:
        await asyncio.sleep(check_interval)
        current_balance = await get_player_balance(receiver_ign)
        if current_balance is None:
            # API temporarily unreachable — keep trying
            continue

        gained = current_balance - baseline_balance
        logger.debug(f"monitor_payment: order={order_id} current={current_balance} gained={gained}")

        if gained >= expected_amount:
            # ── Payment confirmed ──────────────────────────────────────────
            db["building_orders"].update_one(
                {"_id": ObjectId(order_id)},
                {"$set": {"payment_status": "confirmed",
                           "payment_confirmed_time": datetime.now(timezone.utc),
                           "status": "confirmed"}}
            )

            guild = bot.get_guild(guild_id)
            if not guild:
                return

            # Send log to payment log channel
            cfg = db["bot_config"].find_one({"guild_id": guild.id}) or {}
            log_channel_id = cfg.get("PAYMENT_LOG_CHANNEL_ID")
            if log_channel_id:
                log_channel = guild.get_channel(int(log_channel_id))
                if log_channel:
                    log_embed = discord.Embed(
                        title="✅ Payment Received",
                        description=f"**{buyer_ign}** paid **{amount}** to **{receiver_ign}**",
                        color=0x2ecc71,
                        timestamp=datetime.now(timezone.utc)
                    )
                    log_embed.add_field(name="Build", value=build['name'], inline=True)
                    log_embed.add_field(name="Buyer", value=f"<@{buyer_id}>", inline=True)
                    log_embed.set_footer(text=f"Order ID: {order_id}")
                    try:
                        await log_channel.send(embed=log_embed)
                    except Exception as e:
                        logger.error(f"Failed to send payment log: {e}")

            if ticket_channel_id:
                # ── Custom Build: ticket already exists — unlock buyer & post to orders ──
                channel = guild.get_channel(ticket_channel_id)
                buyer = guild.get_member(buyer_id)
                if channel and buyer:
                    try:
                        await channel.set_permissions(buyer, read_messages=True, send_messages=True)
                    except discord.Forbidden:
                        logger.error(f"monitor_payment: could not unlock buyer in {channel.id}")

                    confirmed_embed = discord.Embed(
                        title="✅ Payment Confirmed — API Verified",
                        description=(
                            f"{buyer.mention} Your payment of `{amount}` has been detected "
                            f"and confirmed automatically.\n\n"
                            f"A builder will claim your order shortly!"
                        ),
                        color=0x2ecc71,
                        timestamp=datetime.now(timezone.utc)
                    )
                    try:
                        await channel.send(embed=confirmed_embed)
                    except Exception as e:
                        logger.error(f"monitor_payment: failed to send confirmation embed: {e}")

                try:
                    await post_order_to_builder_channel(bot, ticket_channel_id, guild)
                except Exception as e:
                    logger.error(f"monitor_payment: failed to post to builder channel: {e}")
            else:
                # ── Regular Build: create the ticket channel now ───────────
                buyer = guild.get_member(buyer_id)
                if not buyer:
                    return
                await create_build_ticket_from_modal(bot, guild, buyer, build, modal_data, receiver_ign, amount)
            return

    # ── Expired ────────────────────────────────────────────────────────────
    db["building_orders"].update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"payment_status": "expired", "status": "cancelled"}}
    )

    guild = bot.get_guild(guild_id)

    if ticket_channel_id and guild:
        # Custom Build: close the existing channel with a transcript
        channel = guild.get_channel(ticket_channel_id)
        if channel:
            buyer = guild.get_member(buyer_id)
            buyer_mention = buyer.mention if buyer else f"<@{buyer_id}>"
            try:
                expire_embed = discord.Embed(
                    title="⏰ Payment Timeout — Ticket Closing",
                    description=(
                        f"{buyer_mention} The 30-minute payment window has expired.\n\n"
                        f"**Amount due:** `{amount}`\n"
                        f"**Receiver IGN:** `{receiver_ign}`\n\n"
                        f"No payment was detected. This ticket will now be closed and logged."
                    ),
                    color=0xe74c3c,
                    timestamp=datetime.now(timezone.utc)
                )
                await channel.send(embed=expire_embed)
            except Exception as e:
                logger.error(f"monitor_payment: failed to send expiry embed: {e}")
            await asyncio.sleep(5)
            try:
                await _close_ticket(channel, bot.user, db)
            except Exception as e:
                logger.error(f"monitor_payment: failed to close ticket on expiry: {e}")
    else:
        # Regular Build: DM the buyer
        user = bot.get_user(buyer_id)
        if user:
            try:
                await user.send(
                    f"⏰ Your order for **{build['name']}** expired because payment was not received "
                    f"within 30 minutes. You can re‑apply if you still wish to order."
                )
            except discord.Forbidden:
                pass





# ── Ticket Creation (refactored from modal) ──────────────────────────
async def create_build_ticket_from_modal(bot, guild, buyer, build, modal_data, receiver_ign, amount):
    """
    Creates the ticket channel after payment confirmation.
    Uses the same logic as the original create_build_ticket.
    """
    db = bot.db
    cfg = get_guild_config(db, guild.id)

    trusted_staff_id = cfg.get("TRUSTED_STAFF_ROLE_ID")
    trusted_staff = guild.get_role(trusted_staff_id) if trusted_staff_id else None
    if not trusted_staff:
        logger.error(f"Trusted Staff role not found for guild {guild.id}")
        return

    confirmation_role_id = cfg.get("BUILD_TICKET_PING_ROLE_ID")
    confirmation_role = guild.get_role(confirmation_role_id) if confirmation_role_id else None
    if not confirmation_role:
        confirmation_role = discord.utils.get(guild.roles, name="295")
    if not confirmation_role:
        logger.error(f"Confirmation role (295) not found for guild {guild.id}")
        return

    builder_role = guild.get_role(cfg.get("BUILDER_ROLE_ID")) if cfg.get("BUILDER_ROLE_ID") else None

    cat = discord.utils.get(guild.categories, name="Building")
    if not cat:
        cat = await guild.create_category("Building")
        await cat.set_permissions(guild.default_role, read_messages=False)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        buyer: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        trusted_staff: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        confirmation_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    if builder_role: overwrites[builder_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"build-{buyer.name.lower()}",
        category=cat,
        overwrites=overwrites,
        topic=f"Build: {build['name']} | Buyer: {buyer.name} | IGN: {modal_data['ign']} | Region: {modal_data['region']} | Farm: {modal_data['farm_name']}"
    )

    # Update the order document with ticket channel info and new status
    db["building_orders"].update_one(
        {"guild_id": guild.id, "buyer_id": buyer.id, "build_id": build["id"]},
        {"$set": {
            "ticket_channel_id": channel.id,
            "status": "confirmed",
            "builder_id": None,
            "order_message_id": None,
        }},
        upsert=False
    )

    # Send payment confirmation embed
    fresh_cfg = db["bot_config"].find_one({"guild_id": guild.id}) or {}
    payment_method = fresh_cfg.get("PAYMENT_METHOD") or "your payment method"

    pay_description = f"**Payment received!**\n\n" \
                      f"**Build:** {build['name']}\n**Price:** {build['price']}\n" \
                      f"**IGN:** {modal_data['ign']}\n**Region:** {modal_data['region']}\n" \
                      f"**Farm Name:** {modal_data['farm_name']}\n\n" \
                      f"Now waiting for a builder to claim this order."
    pay_embed = discord.Embed(
        title="✅ Payment Confirmed – Build Order Created",
        description=pay_description,
        color=0x2ecc71
    )
    pay_embed.set_footer(text=f"Order ID: {channel.id}")

    await channel.send(embed=pay_embed)

    close_view = TicketView()
    await channel.send("**Staff Controls**", view=close_view)

    await channel.send(f"{confirmation_role.mention} A new build ticket has been opened!", delete_after=10)
    if builder_role:
        await channel.send(f"{builder_role.mention} New build order! Please review and claim.")

    await buyer.send(f"✅ Your build ticket for **{build['name']}** has been created: {channel.mention}")

    # Post to the builder orders channel so builders can claim it
    try:
        await post_order_to_builder_channel(bot, channel.id, guild)
    except Exception as e:
        logger.error(f"Error posting to builder orders channel: {e}")

# ── Custom Build Ticket Creation (immediate, no payment yet) ─────────
async def create_custom_build_ticket(bot, guild, buyer, build, modal_data):
    """
    Opens a ticket channel immediately for a Custom Build order.
    No payment is required yet — status is 'unpaid' until staff run /build money.
    The channel shows a "Quote Pending" banner and staff instructions.
    """
    db = bot.db
    cfg = get_guild_config(db, guild.id)

    trusted_staff_id = cfg.get("TRUSTED_STAFF_ROLE_ID")
    trusted_staff = guild.get_role(trusted_staff_id) if trusted_staff_id else None
    if not trusted_staff:
        logger.error(f"create_custom_build_ticket: Trusted Staff role not found for guild {guild.id}")
        return

    confirmation_role_id = cfg.get("BUILD_TICKET_PING_ROLE_ID")
    confirmation_role = guild.get_role(confirmation_role_id) if confirmation_role_id else None
    if not confirmation_role:
        confirmation_role = discord.utils.get(guild.roles, name="295")
    if not confirmation_role:
        logger.error(f"create_custom_build_ticket: Confirmation role not found for guild {guild.id}")
        return

    builder_role = guild.get_role(cfg.get("BUILDER_ROLE_ID")) if cfg.get("BUILDER_ROLE_ID") else None

    cat = discord.utils.get(guild.categories, name="Building")
    if not cat:
        cat = await guild.create_category("Building")
        await cat.set_permissions(guild.default_role, read_messages=False)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        # Buyer CAN send messages immediately — staff need to know what they want
        # before a price can even be quoted. They only get locked once the ticket
        # is fully resolved (completed/cancelled/closed).
        buyer: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        trusted_staff: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        confirmation_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if builder_role: overwrites[builder_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"build-{buyer.name.lower()}",
        category=cat,
        overwrites=overwrites,
        topic=(
            f"Build: {build['name']} | Buyer: {buyer.name} | "
            f"IGN: {modal_data['ign']} | Region: {modal_data['region']} | "
            f"Farm: {modal_data['farm_name']}"
        )
    )

    # Update the order document with the ticket channel and set status to unpaid
    db["building_orders"].update_one(
        {"guild_id": guild.id, "buyer_id": buyer.id, "build_id": build["id"],
         "payment_status": "pending"},
        {"$set": {
            "ticket_channel_id": channel.id,
            "status": "unpaid",
            "builder_id": None,
            "order_message_id": None,
        }},
        upsert=False
    )

    # Send the "Quote Pending" info embed visible to buyer and staff
    info_embed = discord.Embed(
        title="🪄 Custom Build — Quote Pending",
        description=(
            f"Hey {buyer.mention}! Your custom build request has been received.\n\n"
            f"**IGN:** `{modal_data['ign']}`\n"
            f"**Region:** `{modal_data['region']}`\n"
            f"**Farm Name:** `{modal_data['farm_name']}`\n\n"
            f"💬 Please describe exactly what you want built (design, size, materials, etc.) below "
            f"— a staff member will review your request and set a price using `/build money`.\n"
            f"Once the price is set you will be pinged with a 30-minute payment window."
        ),
        color=0xf1c40f,
        timestamp=datetime.now(timezone.utc)
    )
    info_embed.set_footer(text="Staff: use /build money to set the price and start the countdown.")
    await channel.send(embed=info_embed)

    close_view = TicketView()
    await channel.send("**Staff Controls**", view=close_view)

    await channel.send(
        f"{confirmation_role.mention} New **Custom Build** ticket opened — please quote a price!",
        delete_after=10
    )

    try:
        await buyer.send(
            f"✅ Your custom build ticket has been created: {channel.mention}\n"
            f"Staff will set a price shortly — you'll be pinged once it's ready."
        )
    except discord.Forbidden:
        pass

    logger.info(f"create_custom_build_ticket: opened #{channel.name} ({channel.id}) for {buyer} in guild {guild.id}")


# ── Modals ─────────────────────────────────────────────────────────────
class BuildOrderModal(discord.ui.Modal, title="Place a Build Order"):
    def __init__(self, build: dict):
        super().__init__()
        self.build = build
        self.add_item(discord.ui.TextInput(label="Your IGN", custom_id="ign", required=True))
        self.add_item(discord.ui.TextInput(label="Region (e.g. EU, NA, ASIA)", custom_id="region", required=True))
        self.add_item(discord.ui.TextInput(label="Farm Name", custom_id="farm_name", required=True, style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction: discord.Interaction):
        ign = self.children[0].value.strip()
        region = self.children[1].value.strip()
        farm_name = self.children[2].value.strip()

        # Always read fresh from DB — get_guild_config may return a cached/stale value
        cfg = interaction.client.db["bot_config"].find_one({"guild_id": interaction.guild.id}) or {}
        receiver_ign = cfg.get("PAYMENT_RECEIVER_IGN")
        if not receiver_ign:
            await interaction.response.send_message(
                "❌ Payment receiver IGN is not configured. Please ask an admin to set it in the dashboard.",
                ephemeral=True
            )
            return

        build = self.build
        amount = build["price"]

        db = interaction.client.db
        order_doc = {
            "guild_id": interaction.guild.id,
            "buyer_id": interaction.user.id,
            "ign": ign,
            "region": region,
            "farm_name": farm_name,
            "build_id": build["id"],
            "build_name": build["name"],
            "price": amount,
            "payment_receiver_ign": receiver_ign,
            "payment_amount": amount,
            "payment_status": "pending",
            "payment_request_time": datetime.now(timezone.utc),
            "payment_check_count": 0,
            "status": "payment_pending",
            "builder_id": None,
            "order_message_id": None,
            "notes": [],
            "created_at": datetime.now(timezone.utc)
        }
        result = db["building_orders"].insert_one(order_doc)
        order_id = result.inserted_id

        is_custom = parse_price(amount) is None  # "Quote Pending" → no numeric price

        if is_custom:
            # Custom Build: open the ticket immediately so staff can quote a price.
            # monitor_payment is NOT started — /build money handles the countdown later.
            await interaction.response.send_message(
                f"✅ Custom build request submitted! Your ticket is being opened now.\n"
                f"Staff will review and set a price shortly.\n\n"
                f"**IGN:** {ign}\n**Region:** {region}\n**Farm:** {farm_name}",
                ephemeral=True
            )
            asyncio.create_task(
                create_custom_build_ticket(
                    interaction.client,
                    interaction.guild,
                    interaction.user,
                    build,
                    {"ign": ign, "region": region, "farm_name": farm_name},
                )
            )
        else:
            # Regular priced build: wait for payment before opening a ticket.
            asyncio.create_task(
                monitor_payment(
                    str(order_id),
                    db,
                    interaction.guild.id,
                    ign,
                    receiver_ign,
                    amount,
                    interaction.user.id,
                    build,
                    {"ign": ign, "region": region, "farm_name": farm_name},
                    interaction.client
                )
            )
            await interaction.response.send_message(
                f"✅ Order placed! Please pay **{amount}** to in‑game player **{receiver_ign}** within 30 minutes.\n"
                f"The bot will automatically open your ticket once payment is confirmed.\n\n"
                f"**IGN:** {ign}\n**Region:** {region}\n**Farm:** {farm_name}",
                ephemeral=True
            )

# ── Views (Payment & Builder Claim) ──────────────────────────────────
class PaymentView(discord.ui.View):
    def __init__(self, buyer_id: int, channel_id: int, confirmation_role: discord.Role):
        super().__init__(timeout=None)
        self.buyer_id = buyer_id
        self.channel_id = channel_id
        self.confirmation_role_id = confirmation_role.id if confirmation_role else None

        paid_btn = discord.ui.Button(label="💰 Paid", style=discord.ButtonStyle.green, custom_id=f"paid_{channel_id}")
        close_btn = discord.ui.Button(label="🔒 Close Ticket", style=discord.ButtonStyle.grey, custom_id=f"close_ticket_{channel_id}")
        paid_btn.callback = self.paid_callback
        close_btn.callback = self.close_callback
        self.add_item(paid_btn)
        self.add_item(close_btn)

    async def paid_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.buyer_id:
            return await interaction.response.send_message("❌ Only the order owner can mark this as paid.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": self.channel_id})
        if not order or parse_price(str(order.get("price", ""))) is None:
            return await interaction.response.send_message(
                "❌ A price hasn't been set for this order yet. Please wait for staff to use `/build money` first.",
                ephemeral=True
            )
        confirmation_role = interaction.guild.get_role(self.confirmation_role_id) if self.confirmation_role_id else None
        confirm_view = ConfirmPaymentView(self.buyer_id, self.channel_id, confirmation_role)
        embed = discord.Embed(
            title="🔐 Confirm Payment",
            description=f"{interaction.user.mention} has marked the order as paid.\nClick **Received** if payment arrived, or **Didn't Receive** to go back.",
            color=0x3498db
        )
        await interaction.response.edit_message(embed=embed, view=confirm_view)

    async def close_callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        confirmation_role = interaction.guild.get_role(self.confirmation_role_id) if self.confirmation_role_id else None
        is_buyer = interaction.user.id == self.buyer_id
        is_staff = interaction.user.guild_permissions.administrator or (confirmation_role and confirmation_role in interaction.user.roles)
        if not is_buyer and not is_staff:
            return await interaction.response.send_message("❌ Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...", ephemeral=True)
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Build ticket closed by {interaction.user}")
        except discord.HTTPException as e:
            print(f"Failed to delete build ticket channel: {e}")

class ConfirmPaymentView(discord.ui.View):
    def __init__(self, buyer_id: int, channel_id: int, confirmation_role: discord.Role):
        super().__init__(timeout=None)
        self.buyer_id = buyer_id
        self.channel_id = channel_id
        self.confirmation_role_id = confirmation_role.id if confirmation_role else None

        self.received_btn = discord.ui.Button(label="✅ Received", style=discord.ButtonStyle.green, custom_id=f"confirm_received_{channel_id}")
        self.deny_btn = discord.ui.Button(label="❌ Didn't Receive", style=discord.ButtonStyle.red, custom_id=f"confirm_deny_{channel_id}")
        self.received_btn.callback = self.received_callback
        self.deny_btn.callback = self.deny_callback
        self.add_item(self.received_btn)
        self.add_item(self.deny_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        confirmation_role = interaction.guild.get_role(self.confirmation_role_id) if self.confirmation_role_id else None
        if confirmation_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only the 295 role can confirm payment.", ephemeral=True)
            return False
        return True

    async def received_callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            return await interaction.response.send_message("❌ Ticket channel not found.", ephemeral=True)

        buyer = interaction.guild.get_member(self.buyer_id)
        if buyer:
            current_overwrites = channel.overwrites_for(buyer)
            current_overwrites.send_messages = True
            current_overwrites.read_messages = True
            try:
                await channel.set_permissions(buyer, overwrite=current_overwrites)
            except discord.Forbidden:
                return await interaction.response.send_message("❌ I lack permission to update the buyer.", ephemeral=True)

        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": self.channel_id})
        db["building_orders"].update_one({"ticket_channel_id": self.channel_id}, {"$set": {"status": "confirmed"}})

        success_embed = discord.Embed(title="✅ Payment Confirmed", description="The buyer can now talk. Please proceed with the build.", color=0x2ecc71)
        await interaction.response.edit_message(embed=success_embed, view=None)

        # Send payment log
        if order:
            try:
                cfg = db["bot_config"].find_one({"guild_id": interaction.guild.id}) or {}
                log_channel_id = cfg.get("PAYMENT_LOG_CHANNEL_ID")
                if log_channel_id:
                    log_channel = interaction.guild.get_channel(int(log_channel_id))
                    if log_channel:
                        log_embed = discord.Embed(
                            title="✅ Payment Received (Manual)",
                            description=f"**{order.get('ign', 'Unknown')}** paid **{order.get('price', 'Unknown')}** — confirmed by {interaction.user.mention}",
                            color=0x2ecc71,
                            timestamp=datetime.now(timezone.utc)
                        )
                        log_embed.add_field(name="Build", value=order.get("build_name", "Unknown"), inline=True)
                        log_embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
                        log_embed.set_footer(text=f"Ticket Channel: {self.channel_id}")
                        await log_channel.send(embed=log_embed)
            except Exception as e:
                logger.error(f"Failed to send manual payment log: {e}")

        try:
            await post_order_to_builder_channel(interaction, self.channel_id)
        except Exception as e:
            print(f"❌ Error posting to builder-orders: {e}")

    async def deny_callback(self, interaction: discord.Interaction):
        confirmation_role = interaction.guild.get_role(self.confirmation_role_id) if self.confirmation_role_id else None
        pay_view = PaymentView(self.buyer_id, self.channel_id, confirmation_role)

        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": self.channel_id})
        price = order["price"] if order else "Unknown"

        embed = discord.Embed(title="🧾 Payment Required", description=f"The payment was not received. Please pay `{price}` and click Paid again.", color=0xf1c40f)
        await interaction.response.edit_message(embed=embed, view=pay_view)

# ── Post order to builder-orders channel ──────────────────────────────
async def post_order_to_builder_channel(interaction_or_bot, ticket_channel_id: int, guild: discord.Guild = None):
    """
    Can be called with (interaction, ticket_channel_id) from button callbacks,
    or with (bot, ticket_channel_id, guild) from the background monitor path.
    """
    if isinstance(interaction_or_bot, discord.Interaction):
        interaction = interaction_or_bot
        guild = interaction.guild
        db = interaction.client.db
    else:
        # Called as (bot, ticket_channel_id, guild)
        bot = interaction_or_bot
        db = bot.db
        # guild is passed explicitly

    order = db["building_orders"].find_one({"ticket_channel_id": ticket_channel_id})
    if not order: return

    orders_channel_id = get_guild_config(db, guild.id).get("BUILDER_ORDERS_CHANNEL_ID")
    if not orders_channel_id: return

    orders_channel = guild.get_channel(orders_channel_id)
    if not orders_channel: return

    ticket_channel = guild.get_channel(ticket_channel_id)
    ticket_channel_name = ticket_channel.name if ticket_channel else f"deleted-{ticket_channel_id}"

    embed = discord.Embed(title=f"🛠️ New Build Order – {order['build_name']}", color=0xf39c12, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="IGN", value=order["ign"], inline=True)
    embed.add_field(name="Region", value=order["region"], inline=True)
    embed.add_field(name="Farm Name", value=order.get("farm_name", "N/A"), inline=True)
    embed.add_field(name="Price", value=order["price"], inline=True)
    embed.set_footer(text=f"Ticket: {ticket_channel_name}")

    view = BuilderClaimView(order)
    msg = await orders_channel.send(embed=embed, view=view)
    db["building_orders"].update_one({"ticket_channel_id": ticket_channel_id}, {"$set": {"order_message_id": msg.id}})

    fresh_cfg = db["bot_config"].find_one({"guild_id": guild.id}) or {}
    ping_role_id = fresh_cfg.get("BUILD_ORDER_PING_ROLE_ID")
    ping_role = guild.get_role(ping_role_id) if ping_role_id else None
    if not ping_role:
        builder_role_id = fresh_cfg.get("BUILDER_ROLE_ID")
        ping_role = guild.get_role(builder_role_id) if builder_role_id else None
    if ping_role:
        await orders_channel.send(f"{ping_role.mention} New build order available! ⬆️")

# ── Builder Claim View ─────────────────────────────────────────────────
class BuilderClaimView(discord.ui.View):
    def __init__(self, order: dict):
        super().__init__(timeout=None)
        self.order = order
        claim_btn = discord.ui.Button(label="🔨 Claim Order", style=discord.ButtonStyle.green, custom_id=f"claim_{order['ticket_channel_id']}")
        claim_btn.callback = self.claim_callback
        self.add_item(claim_btn)

    async def claim_callback(self, interaction: discord.Interaction):
        if not is_builder(interaction):
            return await interaction.response.send_message("❌ Only builders can claim orders.", ephemeral=True)

        db = interaction.client.db
        current = db["building_orders"].find_one({"ticket_channel_id": self.order["ticket_channel_id"]})
        if not current:
            return await interaction.response.send_message("❌ Order not found in database.", ephemeral=True)
        if current.get("builder_id"):
            return await interaction.response.send_message("❌ This order has already been claimed.", ephemeral=True)

        ticket_ch = interaction.guild.get_channel(current["ticket_channel_id"])
        if not ticket_ch:
            return await interaction.response.send_message("❌ Ticket channel not found.", ephemeral=True)

        await ticket_ch.set_permissions(interaction.user, read_messages=True, send_messages=True)
        db["building_orders"].update_one(
            {"ticket_channel_id": current["ticket_channel_id"]},
            {"$set": {"builder_id": interaction.user.id, "status": "claimed"}}
        )

        embed = interaction.message.embeds[0]
        embed.add_field(name="Claimed By", value=interaction.user.mention, inline=False)
        embed.color = discord.Color.green()
        await interaction.message.edit(embed=embed, view=None)

        await interaction.response.send_message(f"✅ Order claimed by {interaction.user.mention}.", ephemeral=True)
        await ticket_ch.send(f"🔨 {interaction.user.mention} has claimed this build order. You can now coordinate.")

# ── Build Panel View & Dropdown ──────────────────────────────────────
class BuildPanelView(discord.ui.View):
    def __init__(self, builds: list):
        super().__init__(timeout=None)
        if builds:
            options = [
                discord.SelectOption(
                    label=b["name"],
                    description=f"Price: {b['price']}",
                    value=b["id"],
                    emoji=b.get("emoji", "🧱")
                ) for b in builds
            ]
            self.add_item(BuildDropdown(options, builds))
        self.add_item(CustomBuildButton())

class BuildDropdown(discord.ui.Select):
    def __init__(self, options: list, builds: list):
        super().__init__(
            placeholder="Choose a build package...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.builds = {b["id"]: b for b in builds}

    async def callback(self, interaction: discord.Interaction):
        build = self.builds.get(self.values[0])
        if not build:
            return await interaction.response.send_message("❌ Build not found.", ephemeral=True)
        modal = BuildOrderModal(build)
        await interaction.response.send_modal(modal)

class CustomBuildButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Custom Build",
            style=discord.ButtonStyle.success,
            custom_id="custom_build_btn",
            emoji="🪄"
        )
        self.custom_build_dict = {
            "id": "custom",
            "name": "Custom Build",
            "price": "Quote Pending",
            "emoji": "🪄"
        }

    async def callback(self, interaction: discord.Interaction):
        modal = BuildOrderModal(self.custom_build_dict)
        await interaction.response.send_modal(modal)

# ── Cog ────────────────────────────────────────────────────────────────
class Building(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.loop.create_task(self._restore_when_ready())

    async def _restore_when_ready(self):
        await self.bot.wait_until_ready()
        if not hasattr(self.bot, 'db') or self.bot.db is None:
            logger.error("Building cog: db not available, skipping view restore")
            return
        await self.restore_panel_views()

    async def restore_panel_views(self):
        for order in self.bot.db["building_orders"].find({"status": {"$in": ["unpaid", "confirmed", "claimed", "payment_pending"]}}):
            guild = self.bot.get_guild(order["guild_id"])
            if not guild:
                continue
            cfg = self.bot.db["bot_config"].find_one({"guild_id": guild.id}) or {}
            confirmation_role_id = cfg.get("BUILD_TICKET_PING_ROLE_ID")
            confirmation_role = guild.get_role(confirmation_role_id) if confirmation_role_id else None

            if order["status"] == "payment_pending":
                request_time = order.get("payment_request_time")
                if request_time and (datetime.now(timezone.utc) - request_time) < timedelta(minutes=30):
                    asyncio.create_task(
                        monitor_payment(
                            str(order["_id"]),
                            self.bot.db,
                            guild.id,
                            order["ign"],
                            order.get("payment_receiver_ign"),
                            order["price"],
                            order["buyer_id"],
                            {"id": order["build_id"], "name": order["build_name"], "price": order["price"]},
                            {"ign": order["ign"], "region": order["region"], "farm_name": order["farm_name"]},
                            self.bot
                        )
                    )
            elif order["status"] in ("unpaid", "confirmed", "claimed"):
                if order["status"] == "unpaid":
                    # Custom builds awaiting a price have a non-numeric price ("Quote Pending").
                    # They use TicketView (staff controls only) — PaymentView would be wrong here
                    # because there's no price set yet. Regular priced unpaid orders use PaymentView.
                    if parse_price(str(order.get("price", ""))) is None:
                        view = TicketView()
                    else:
                        view = PaymentView(order["buyer_id"], order["ticket_channel_id"], confirmation_role)
                    self.bot.add_view(view)
                else:
                    view = BuilderClaimView(order)
                    self.bot.add_view(view)

        active_channel_ids = set(
            doc["ticket_channel_id"]
            for doc in self.bot.db["building_orders"].find(
                {"status": {"$in": ["unpaid", "confirmed", "claimed"]}},
                {"ticket_channel_id": 1, "guild_id": 1}
            )
        )
        for guild in self.bot.guilds:
            cfg = self.bot.db["bot_config"].find_one({"guild_id": guild.id}) or {}
            trusted_staff_id = cfg.get("TRUSTED_STAFF_ROLE")
            trusted_staff = guild.get_role(trusted_staff_id) if trusted_staff_id else None
            builder_role = guild.get_role(cfg.get("BUILDER_ROLE_ID")) if cfg.get("BUILDER_ROLE_ID") else None
            for channel in guild.text_channels:
                if channel.id not in active_channel_ids:
                    continue
                try:
                    if trusted_staff: await channel.set_permissions(trusted_staff, read_messages=True, send_messages=True)
                    if builder_role: await channel.set_permissions(builder_role, read_messages=True, send_messages=True)
                except discord.Forbidden:
                    pass

    @app_commands.command(name="buildpanel", description="Post/update the build ordering panel")
    @admin_only()
    async def buildpanel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        db = self.bot.db
        panel = db["building_panels"].find_one({"guild_id": interaction.guild.id})
        if not panel or not panel.get("builds"):
            return await interaction.edit_original_response(content="❌ No builds configured. Set them up in the dashboard first.")

        builds = panel["builds"]

        desc_lines = ["Select a build from the dropdown below to place your order.\n\n**Available Builds:**\n"]
        for b in builds:
            emoji = b.get("emoji", "🧱")
            desc_lines.append(f"{emoji} **{b['name']}** - `{b['price']}`")
        desc_lines.append(f"\n🪄 **Custom Build** - `Quote Pending` (Select the custom button)")

        embed = discord.Embed(
            title="🏗️ Build Orders",
            description="\n".join(desc_lines),
            color=0x5865F2
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        original_msg = await interaction.original_response()
        async for msg in interaction.channel.history(limit=20):
            if msg.author == self.bot.user and msg.id != original_msg.id:
                try: await msg.delete()
                except discord.HTTPException: pass

        view = BuildPanelView(builds)
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    # ── Slash Commands ────────────────────────────────────────────────────
    build_group = app_commands.Group(name="build", description="Manage build tickets", guild_only=True)

    @build_group.command(name="paid", description="Mark a build ticket as paid (bypasses button)")
    async def build_paid(self, interaction: discord.Interaction):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build paid")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)
        if order["status"] == "confirmed":
            return await interaction.response.send_message("❌ This order is already confirmed.", ephemeral=True)
        if order["status"] in ("completed", "cancelled"):
            return await interaction.response.send_message(f"❌ This order is already `{order['status']}` and cannot be paid.", ephemeral=True)
        if parse_price(str(order.get("price", ""))) is None:
            return await interaction.response.send_message(
                "❌ No price has been set on this order yet — use `/build money` to set a price first.",
                ephemeral=True
            )
        buyer = interaction.guild.get_member(order["buyer_id"])
        if buyer:
            overwrites = interaction.channel.overwrites_for(buyer)
            overwrites.send_messages = True
            await interaction.channel.set_permissions(buyer, overwrite=overwrites)
        db["building_orders"].update_one({"ticket_channel_id": interaction.channel.id}, {"$set": {"status": "confirmed"}})

        # Send payment log
        try:
            cfg = db["bot_config"].find_one({"guild_id": interaction.guild.id}) or {}
            log_channel_id = cfg.get("PAYMENT_LOG_CHANNEL_ID")
            if log_channel_id:
                log_channel = interaction.guild.get_channel(int(log_channel_id))
                if log_channel:
                    log_embed = discord.Embed(
                        title="✅ Payment Received (Staff Override)",
                        description=f"**{order.get('ign', 'Unknown')}** — payment manually confirmed by {interaction.user.mention}",
                        color=0x2ecc71,
                        timestamp=datetime.now(timezone.utc)
                    )
                    log_embed.add_field(name="Build", value=order.get("build_name", "Unknown"), inline=True)
                    log_embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
                    log_embed.add_field(name="Price", value=order.get("price", "Unknown"), inline=True)
                    log_embed.set_footer(text=f"Ticket Channel: {interaction.channel.id}")
                    await log_channel.send(embed=log_embed)
        except Exception as e:
            logger.error(f"Failed to send paid log: {e}")

        embed = discord.Embed(title="✅ Payment Manually Confirmed", description="Order is now confirmed and buyer can speak.", color=0x2ecc71)
        await interaction.response.send_message(embed=embed)
        try:
            await post_order_to_builder_channel(interaction, interaction.channel.id)
        except Exception as e:
            logger.error(f"Error posting to builder channel: {e}")

    @build_group.command(name="money", description="Set the money owed on a ticket")
    @app_commands.describe(amount="The new price/amount owed (e.g. '500k' or '$10')")
    async def build_money(self, interaction: discord.Interaction, amount: str):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build money")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)

        is_custom = order.get("build_name", "").lower() == "custom build" or \
                    order.get("build_id", "") == "custom" or \
                    str(order.get("price", "")).lower() in ("quote pending", "")

        db["building_orders"].update_one(
            {"ticket_channel_id": interaction.channel.id},
            {"$set": {"price": amount, "payment_status": "pending_custom_countdown"}}
        )

        if is_custom:
            # --- Custom Build: start the API payment monitor (same as regular builds) ---
            cfg = db["bot_config"].find_one({"guild_id": interaction.guild.id}) or {}
            receiver_ign = cfg.get("PAYMENT_RECEIVER_IGN", "the receiver")

            buyer_id = order.get("buyer_id")
            buyer = interaction.guild.get_member(buyer_id) if buyer_id else None
            buyer_mention = buyer.mention if buyer else (f"<@{buyer_id}>" if buyer_id else "the buyer")

            # Unix timestamp 30 minutes from now for Discord countdown
            deadline_ts = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())

            countdown_embed = discord.Embed(
                title="💰 Payment Required — 30 Minutes to Pay",
                description=(
                    f"{buyer_mention} A price has been set for your Custom Build order!\n\n"
                    f"**Amount:** `{amount}`\n"
                    f"**Pay in-game to:** `{receiver_ign}`\n\n"
                    f"⏳ You have until <t:{deadline_ts}:T> (<t:{deadline_ts}:R>) to complete payment.\n\n"
                    f"The bot will automatically detect your payment via the API. "
                    f"If payment is not received in time, this ticket will be automatically closed."
                ),
                color=0xf1c40f,
                timestamp=datetime.now(timezone.utc)
            )
            countdown_embed.set_footer(text="Payment is verified automatically — no need for staff to confirm.")

            await interaction.response.send_message(
                content=buyer_mention,
                embed=countdown_embed,
            )

            # Store when the countdown started
            db["building_orders"].update_one(
                {"ticket_channel_id": interaction.channel.id},
                {"$set": {"custom_countdown_started": datetime.now(timezone.utc),
                           "payment_receiver_ign": receiver_ign,
                           "payment_request_time": datetime.now(timezone.utc)}}
            )

            # Launch the API monitor — passes the existing channel so it unlocks
            # the buyer in-place instead of creating a new ticket channel
            asyncio.create_task(
                monitor_payment(
                    str(order["_id"]),
                    db,
                    interaction.guild.id,
                    order.get("ign", ""),
                    receiver_ign,
                    amount,
                    buyer_id,
                    {"id": order.get("build_id", "custom"),
                     "name": order.get("build_name", "Custom Build"),
                     "price": amount},
                    {"ign": order.get("ign", ""), "region": order.get("region", ""),
                     "farm_name": order.get("farm_name", "")},
                    interaction.client,
                    ticket_channel_id=interaction.channel.id,
                )
            )
        else:
            # --- Regular build: just update price silently ---
            await interaction.response.send_message(f"💰 Price updated to `{amount}` for this order.")

    @build_group.command(name="claim", description="Claim a build ticket for yourself")
    async def build_claim(self, interaction: discord.Interaction):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build claim")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)
        if order["status"] in ("completed", "cancelled"):
            return await interaction.response.send_message(f"❌ This order is already `{order['status']}`.", ephemeral=True)
        if order["status"] not in ("confirmed", "unpaid"):
            return await interaction.response.send_message("❌ This order cannot be claimed yet — payment has not been confirmed.", ephemeral=True)
        # Extra guard: unpaid orders with a non-numeric or unset price are custom builds still awaiting a quote
        if order["status"] == "unpaid" and parse_price(str(order.get("price", ""))) is None:
            return await interaction.response.send_message(
                "❌ This custom build hasn't been paid yet — use `/build money` to set a price first, "
                "then confirm payment before claiming.",
                ephemeral=True
            )
        if order.get("builder_id"):
            claimer = interaction.guild.get_member(order["builder_id"])
            name = claimer.mention if claimer else f"<@{order['builder_id']}>"
            return await interaction.response.send_message(f"❌ This order is already claimed by {name}.", ephemeral=True)
        await interaction.channel.set_permissions(interaction.user, read_messages=True, send_messages=True)
        db["building_orders"].update_one({"ticket_channel_id": interaction.channel.id}, {"$set": {"builder_id": interaction.user.id, "status": "claimed"}})
        await interaction.response.send_message(f"🔨 {interaction.user.mention} has claimed this build order.")

    @build_group.command(name="complete", description="Mark a build ticket as completed")
    async def build_complete(self, interaction: discord.Interaction):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build complete")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)
        if order["status"] == "completed":
            return await interaction.response.send_message("❌ This order is already marked as completed.", ephemeral=True)
        if order["status"] == "cancelled":
            return await interaction.response.send_message("❌ This order was cancelled and cannot be completed.", ephemeral=True)
        if order["status"] not in ("claimed", "confirmed"):
            return await interaction.response.send_message("❌ This order must be claimed before it can be completed.", ephemeral=True)
        db["building_orders"].update_one({"ticket_channel_id": interaction.channel.id}, {"$set": {"status": "completed"}})
        embed = discord.Embed(title="🎉 Build Completed", description="This order has been marked as completed. Generating transcript and closing in 5 seconds...", color=0x2ecc71)
        await interaction.response.send_message(embed=embed)
        await asyncio.sleep(5)
        try:
            await _close_ticket(interaction.channel, interaction.user, db)
        except Exception as e:
            logger.error(f"build_complete: failed to close ticket via _close_ticket: {e}")
            try:
                await interaction.channel.delete(reason="Build completed")
            except Exception:
                pass

    @build_group.command(name="cancel", description="Cancel and close a build ticket")
    async def build_cancel(self, interaction: discord.Interaction):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build cancel")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)
        if order["status"] == "cancelled":
            return await interaction.response.send_message("❌ This order is already cancelled.", ephemeral=True)
        if order["status"] == "completed":
            return await interaction.response.send_message("❌ This order is already completed and cannot be cancelled.", ephemeral=True)
        db["building_orders"].update_one({"ticket_channel_id": interaction.channel.id}, {"$set": {"status": "cancelled"}})
        await interaction.response.send_message("❌ Ticket cancelled. Generating transcript and closing in 3 seconds...")
        await asyncio.sleep(3)
        try:
            await _close_ticket(interaction.channel, interaction.user, db)
        except Exception as e:
            logger.error(f"build_cancel: failed to close ticket via _close_ticket: {e}")
            try:
                await interaction.channel.delete(reason="Build cancelled")
            except Exception:
                pass

    @build_group.command(name="addnote", description="Add a staff note to the build ticket")
    @app_commands.describe(note="The note to add to the ticket logs")
    async def build_addnote(self, interaction: discord.Interaction, note: str):
        if not (is_builder(interaction) or has_cmd_perm(interaction, "build addnote")):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        db = interaction.client.db
        order = db["building_orders"].find_one({"ticket_channel_id": interaction.channel.id})
        if not order:
            return await interaction.response.send_message("❌ This is not a valid build ticket channel.", ephemeral=True)
        note_doc = {
            "author": interaction.user.display_name,
            "content": note,
            "at": datetime.now(timezone.utc)
        }
        db["building_orders"].update_one({"ticket_channel_id": interaction.channel.id}, {"$push": {"notes": note_doc}})
        embed = discord.Embed(title="📝 Note Added", description=note, color=0x5865F2, timestamp=note_doc["at"])
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        cfg = self.bot.db["bot_config"].find_one({"guild_id": after.guild.id}) or {}
        builder_role_id = cfg.get("BUILDER_ROLE_ID")
        builder_role = after.guild.get_role(builder_role_id) if builder_role_id else None
        trusted_staff_id = cfg.get("TRUSTED_STAFF_ROLE")
        trusted_staff = after.guild.get_role(trusted_staff_id) if trusted_staff_id else None

        gained_builder = builder_role and builder_role in after.roles and builder_role not in before.roles
        gained_trusted = trusted_staff and trusted_staff in after.roles and trusted_staff not in before.roles

        if gained_builder or gained_trusted:
            for ch in after.guild.text_channels:
                if ch.name.startswith("build-"):
                    try: await ch.set_permissions(after, read_messages=True, send_messages=True)
                    except Exception: pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Building(bot))
