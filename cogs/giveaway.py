import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta, timezone
import random
import string
from typing import Optional, List
from cogs.config import admin_only, is_admin_user, is_staff_user, get_guild_config, TICKET_PREFIXES
from cogs.tickets import TicketView


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def ensure_aware(dt: datetime) -> datetime:
    """Return a UTC-aware datetime, converting naive ones if needed."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


import re

_DURATION_RE = re.compile(r"\s*(\d+)\s*([dhms])\s*", re.IGNORECASE)


def parse_duration(text: str) -> Optional[int]:
    """Parse a duration string like '1h', '30m', '2d', '45s', or a combo like
    '1d12h' into a total number of seconds. Returns None if the string doesn't
    parse cleanly (unknown unit, no number, leftover junk, etc.)."""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    pos = 0
    matched_any = False

    for match in _DURATION_RE.finditer(text):
        if match.start() != pos:
            return None  # gap/garbage between matches (e.g. "1h x 30m")
        amount = int(match.group(1))
        unit = match.group(2).lower()
        total += amount * unit_seconds[unit]
        pos = match.end()
        matched_any = True

    if not matched_any or pos != len(text):
        return None  # nothing matched, or trailing junk after the last match

    return total


def generate_display_id(length: int = 10) -> str:
    """Short cosmetic ID shown on the giveaway embed (not the Discord message ID)."""
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(random.choices(alphabet, k=length))


def is_claim_ticket(channel: discord.abc.GuildChannel) -> bool:
    """True if this channel is a prize-claim ticket created by WinnerClaimView."""
    name = getattr(channel, "name", "")
    return name.startswith("claim-")


def get_claim_winner(channel: discord.TextChannel) -> Optional[discord.Member]:
    """The winner is the only Member (as opposed to Role) with an explicit
    permission overwrite on a claim ticket — see WinnerClaimView.create_claim_ticket."""
    for target, _overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member):
            return target
    return None


# ---------------------------------------------------------------------------
# Data persistence (MongoDB)
# ---------------------------------------------------------------------------

class GiveawayData:
    def __init__(self, db):
        self.db = db
        self.active_giveaways = {}
        self.load_data()

    def load_data(self):
        try:
            collection = self.db["giveaways"]
            for doc in collection.find():
                msg_id = doc["message_id"]
                giveaway = Giveaway.from_dict(doc["giveaway_data"])
                self.active_giveaways[msg_id] = giveaway
            print(f"✅ Loaded {len(self.active_giveaways)} giveaways from MongoDB")
        except Exception as e:
            print(f"❌ Failed to load giveaways: {e}")

    def add_giveaway(self, message_id: int, giveaway):
        self.active_giveaways[message_id] = giveaway
        try:
            self.db["giveaways"].update_one(
                {"message_id": message_id},
                {"$set": {"giveaway_data": giveaway.to_dict()}},
                upsert=True
            )
        except Exception as e:
            print(f"❌ Failed to save giveaway to DB: {e}")

    def remove_giveaway(self, message_id: int):
        if message_id in self.active_giveaways:
            del self.active_giveaways[message_id]
        try:
            self.db["giveaways"].delete_one({"message_id": message_id})
        except Exception as e:
            print(f"❌ Failed to remove giveaway from DB: {e}")


# ---------------------------------------------------------------------------
# Sponsor persistence (MongoDB) — tracks who is paying out a prize claim
# ---------------------------------------------------------------------------

class GiveawaySponsors:
    """One sponsor per claim ticket channel. Sponsor is responsible for
    paying out the giveaway winner in that ticket."""

    def __init__(self, db):
        self.db = db

    def get(self, channel_id: int) -> Optional[dict]:
        if self.db is None:
            return None
        try:
            return self.db["giveaway_sponsors"].find_one({"channel_id": channel_id})
        except Exception as e:
            print(f"❌ Failed to fetch sponsor: {e}")
            return None

    def set(self, channel_id: int, guild_id: int, sponsor_id: int) -> bool:
        if self.db is None:
            return False
        try:
            self.db["giveaway_sponsors"].update_one(
                {"channel_id": channel_id},
                {"$set": {"channel_id": channel_id, "guild_id": guild_id, "sponsor_id": sponsor_id}},
                upsert=True,
            )
            return True
        except Exception as e:
            print(f"❌ Failed to set sponsor: {e}")
            return False

    def remove(self, channel_id: int) -> bool:
        if self.db is None:
            return False
        try:
            self.db["giveaway_sponsors"].delete_one({"channel_id": channel_id})
            return True
        except Exception as e:
            print(f"❌ Failed to remove sponsor: {e}")
            return False


# ---------------------------------------------------------------------------
# Giveaway model
# ---------------------------------------------------------------------------

class Giveaway:
    def __init__(self, channel_id: int, end_time: datetime, prize: str,
                 winners_count: int, title: str, description: str,
                 host_id: int, message_id: int = None, claim_time_seconds: int = 600,
                 preset_winner_id: Optional[int] = None, display_id: Optional[str] = None):
        self.channel_id = channel_id
        self.end_time = ensure_aware(end_time)
        self.prize = prize
        self.winners_count = winners_count
        self.title = title
        self.description = description
        self.host_id = host_id
        self.message_id = message_id
        self.claim_time_seconds = claim_time_seconds
        self.entries = []
        self.ended = False
        self.winners = []
        self.announcement_message_id = None
        self.claim_end_time = None
        self.claimed_users = set()
        # Maps str(winner_id) -> claim ticket channel_id, so re-open checks can
        # verify whether that specific channel still exists (rename-proof and
        # survives the ticket being renamed, unlike a name-based lookup).
        self.claim_channels = {}
        # If set, this user wins regardless of entries — used by /giveaway create
        # which takes a specific winner up front instead of picking one at random.
        self.preset_winner_id = preset_winner_id
        # Cosmetic short ID shown on the embed (not the real Discord message ID).
        self.display_id = display_id or generate_display_id()

    def to_dict(self):
        return {
            'channel_id': self.channel_id,
            'end_time': ensure_aware(self.end_time).isoformat(),
            'prize': self.prize,
            'winners_count': self.winners_count,
            'title': self.title,
            'description': self.description,
            'host_id': self.host_id,
            'message_id': self.message_id,
            'entries': self.entries,
            'ended': self.ended,
            'claim_time_seconds': self.claim_time_seconds,
            'winners': self.winners,
            'announcement_message_id': self.announcement_message_id,
            'claim_end_time': ensure_aware(self.claim_end_time).isoformat() if self.claim_end_time else None,
            'claimed_users': list(self.claimed_users),
            'claim_channels': self.claim_channels,
            'preset_winner_id': self.preset_winner_id,
            'display_id': self.display_id,
        }

    @classmethod
    def from_dict(cls, data):
        giveaway = cls(
            channel_id=data['channel_id'],
            end_time=ensure_aware(datetime.fromisoformat(data['end_time'])),
            prize=data['prize'],
            winners_count=data['winners_count'],
            title=data['title'],
            description=data['description'],
            host_id=data['host_id'],
            message_id=data.get('message_id'),
            claim_time_seconds=data.get('claim_time_seconds', 600),
            preset_winner_id=data.get('preset_winner_id'),
            display_id=data.get('display_id'),
        )
        giveaway.entries = data.get('entries', [])
        giveaway.ended = data.get('ended', False)
        giveaway.winners = data.get('winners', [])
        giveaway.announcement_message_id = data.get('announcement_message_id')
        giveaway.claimed_users = set(data.get('claimed_users', []))
        giveaway.claim_channels = data.get('claim_channels', {})

        claim_end = data.get('claim_end_time')
        giveaway.claim_end_time = ensure_aware(datetime.fromisoformat(claim_end)) if claim_end else None

        return giveaway

    def add_entry(self, user_id: int):
        if user_id not in self.entries:
            self.entries.append(user_id)
            return True
        return False

    def pick_winners(self) -> List[int]:
        if not self.entries:
            return []
        unique_entries = list(set(self.entries))
        if len(unique_entries) <= self.winners_count:
            return unique_entries
        return random.sample(unique_entries, self.winners_count)


# ---------------------------------------------------------------------------
# Embed builders (match the "prize as title" giveaway card style)
# ---------------------------------------------------------------------------

def build_running_embed(giveaway: "Giveaway") -> discord.Embed:
    end_ts = int(ensure_aware(giveaway.end_time).timestamp())
    description = (
        f"Ends: <t:{end_ts}:R> (<t:{end_ts}:F>)\n"
        f"Hosted by: <@{giveaway.host_id}>\n"
        f"Entries: **{len(giveaway.entries)}**\n"
        f"Winners: **{giveaway.winners_count}**\n"
        f"ID: {giveaway.display_id}"
    )
    embed = discord.Embed(
        title=giveaway.prize,
        description=description,
        color=0x2B2D31,
        timestamp=datetime.now(timezone.utc),
    )
    return embed


def build_ended_embed(giveaway: "Giveaway", winner_ids: List[int]) -> discord.Embed:
    end_ts = int(ensure_aware(giveaway.end_time).timestamp())
    winner_text = ", ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No winners"
    description = (
        f"Ended: <t:{end_ts}:R> (<t:{end_ts}:F>)\n"
        f"Hosted by: <@{giveaway.host_id}>\n"
        f"Prize: **{giveaway.prize}**\n"
        f"Entries: **{len(giveaway.entries)}**\n"
        f"Winners: {winner_text}\n"
        f"ID: {giveaway.display_id}"
    )
    embed = discord.Embed(
        title=giveaway.prize,
        description=description,
        color=0x2B2D31,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="🔴 Ended")
    return embed


# ---------------------------------------------------------------------------
# Claim IGN Modal
# ---------------------------------------------------------------------------

class ClaimIGNModal(discord.ui.Modal, title="Claim Your Prize"):
    mc_ign = discord.ui.TextInput(
        label="What is your Minecraft IGN?",
        placeholder="e.g. Notch",
        required=True,
        max_length=32,
    )

    def __init__(self, claim_view: "WinnerClaimView"):
        super().__init__()
        self.claim_view = claim_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.claim_view.create_claim_ticket(
            interaction, interaction.user.id, mc_ign=str(self.mc_ign.value).strip()
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"ClaimIGNModal error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong submitting your claim.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong submitting your claim.", ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Winner Claim View
# ---------------------------------------------------------------------------

class WinnerClaimView(discord.ui.View):
    def __init__(
        self,
        giveaway_data: GiveawayData,
        winners: List[int],
        prize: str,
        giveaway_channel_id: int,
        giveaway_message_id: int,
        claim_end_time: datetime,
    ):
        super().__init__(timeout=None)

        self.giveaway_data = giveaway_data
        self.winners = winners
        self.prize = prize
        self.giveaway_channel_id = giveaway_channel_id
        self.giveaway_message_id = giveaway_message_id
        self.claim_end_time = ensure_aware(claim_end_time)
        self.claimed_users = set()

        button = discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.green,
            custom_id=f"claim_{giveaway_message_id}",
        )
        button.callback = self.claim_button_callback
        self.add_item(button)

    def _is_expired(self) -> bool:
        try:
            return datetime.now(timezone.utc) > ensure_aware(self.claim_end_time)
        except Exception as e:
            print(f"Claim expiry check error: {e}")
            return True

    async def _mark_expired(self, message: discord.Message):
        try:
            if message.embeds:
                embed = message.embeds[0]
                if "expired" not in (embed.description or "").lower():
                    embed.description = (embed.description or "") + "\n\n⏰ Claim period has expired."
                    embed.color = discord.Color.red()
                await message.edit(embed=embed, view=None)
        except Exception as e:
            print(f"Failed to mark claim expired on message: {e}")

    async def claim_button_callback(self, interaction: discord.Interaction):
        if self._is_expired():
            await interaction.response.send_message("❌ The claim period has expired!", ephemeral=True)
            await self._mark_expired(interaction.message)
            return

        if interaction.user.id not in self.winners:
            await interaction.response.send_message("❌ You are not one of the giveaway winners!", ephemeral=True)
            return

        # Check DB claimed users or runtime claimed users
        giveaway_obj = self.giveaway_data.active_giveaways.get(self.giveaway_message_id)
        if interaction.user.id in self.claimed_users or (giveaway_obj and interaction.user.id in giveaway_obj.claimed_users):
            await interaction.response.send_message("❌ You already claimed your prize!", ephemeral=True)
            return

        # Open the IGN modal instead of creating the ticket immediately
        modal = ClaimIGNModal(claim_view=self)
        await interaction.response.send_modal(modal)

    async def create_claim_ticket(self, interaction: discord.Interaction, winner_id: int, mc_ign: str = None):
        guild = interaction.guild
        uname = interaction.user.name.lower()

        giveaway_obj = self.giveaway_data.active_giveaways.get(self.giveaway_message_id)
        existing_channel_id = giveaway_obj.claim_channels.get(str(winner_id)) if giveaway_obj else None
        if existing_channel_id:
            existing_channel = guild.get_channel(existing_channel_id)
            if existing_channel:
                await interaction.followup.send(
                    f"❌ You already have an open claim ticket: {existing_channel.mention}", ephemeral=True,
                )
                return
            # Stored channel no longer exists (deleted/closed) — fall through and
            # let them open a fresh one; the stale ID gets overwritten below.

        claim_category = discord.utils.get(guild.categories, name="Claim Tickets")
        if not claim_category:
            claim_category = await guild.create_category("Claim Tickets")
            await claim_category.set_permissions(guild.default_role, read_messages=False)

        # Winner can read/see the ticket and attach proof if needed, but cannot send
        # messages until staff take over and grant permission.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(
                read_messages=True, send_messages=False,
                read_message_history=True, attach_files=False,
            ),
        }

        staff_role_name = get_guild_config(interaction.client.db, guild.id)["STAFF_ROLE"]
        staff_role = discord.utils.get(guild.roles, name=staff_role_name)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                read_message_history=True, attach_files=True,
            )

        ticket = await guild.create_text_channel(
            name=f"claim-{uname}",
            category=claim_category,
            overwrites=overwrites,
            topic=f"Ticket by {interaction.user.name} | Prize Claim",
        )

        giveaway_link = (
            f"https://discord.com/channels/{guild.id}/"
            f"{self.giveaway_channel_id}/{self.giveaway_message_id}"
        )

        embed = discord.Embed(
            title="🎉 Prize Claim",
            description=(
                f"### Welcome {interaction.user.mention}!\n\n"
                f"**Prize:** {self.prize}\n\n"
                f"**Minecraft IGN:** {mc_ign}\n\n"
                f"🔗 **Proof/Original Giveaway:** [Click Here]({giveaway_link})\n\n"
                f"Please wait for staff to process your claim.\n\n"
                f"━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Category",   value="Prize Claim",            inline=True)
        embed.add_field(name="Minecraft IGN", value=mc_ign or "N/A",       inline=True)
        embed.set_footer(text=f"Channel ID: {ticket.id}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        
        view = TicketView()
        await ticket.send(embed=embed, view=view)

        if staff_role:
            await ticket.send(
                f"{staff_role.mention} New giveaway prize claim from {interaction.user.mention}!"
            )

        self.claimed_users.add(winner_id)
        
        # PERSIST THE CLAIM TO DATABASE
        if self.giveaway_message_id in self.giveaway_data.active_giveaways:
            giveaway_obj = self.giveaway_data.active_giveaways[self.giveaway_message_id]
            giveaway_obj.claimed_users.add(winner_id)
            giveaway_obj.claim_channels[str(winner_id)] = ticket.id
            self.giveaway_data.add_giveaway(self.giveaway_message_id, giveaway_obj)

        await interaction.followup.send(
            f"✅ Claim ticket created: {ticket.mention}", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Giveaway Enter Button / View
# ---------------------------------------------------------------------------

class GiveawayButton(discord.ui.Button):
    def __init__(self, giveaway_data: GiveawayData, giveaway: Giveaway):
        super().__init__(
            emoji="🚀",
            style=discord.ButtonStyle.blurple,
            custom_id=f"enter_{giveaway.message_id}",
        )
        self.giveaway_data = giveaway_data
        self.giveaway = giveaway

    async def callback(self, interaction: discord.Interaction):
        if self.giveaway.ended or datetime.now(timezone.utc) > ensure_aware(self.giveaway.end_time):
            await interaction.response.send_message("❌ This giveaway has already ended!", ephemeral=True)
            return

        if self.giveaway.add_entry(interaction.user.id):
            self.giveaway_data.add_giveaway(self.giveaway.message_id, self.giveaway)
            await interaction.response.send_message("✅ You have entered the giveaway! Good luck! 🎉", ephemeral=True)

            try:
                channel = interaction.client.get_channel(self.giveaway.channel_id)
                if channel:
                    msg = await channel.fetch_message(self.giveaway.message_id)
                    await msg.edit(embed=build_running_embed(self.giveaway))
            except Exception as e:
                print(f"Failed to update entry count: {e}")
        else:
            await interaction.response.send_message("❌ You have already entered this giveaway!", ephemeral=True)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_data: GiveawayData, giveaway: Giveaway):
        super().__init__(timeout=None)
        self.add_item(GiveawayButton(giveaway_data, giveaway))


# ---------------------------------------------------------------------------
# Giveaway Cog
# ---------------------------------------------------------------------------

class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.giveaway_data = GiveawayData(self.bot.db)
        self.sponsors = GiveawaySponsors(self.bot.db)

        for msg_id, giveaway in self.giveaway_data.active_giveaways.items():
            if not giveaway.ended:
                view = GiveawayView(self.giveaway_data, giveaway)
                bot.add_view(view)
            elif giveaway.ended and giveaway.claim_end_time and giveaway.winners:
                if datetime.now(timezone.utc) < ensure_aware(giveaway.claim_end_time):
                    view = WinnerClaimView(
                        giveaway_data=self.giveaway_data,
                        winners=giveaway.winners,
                        prize=giveaway.prize,
                        giveaway_channel_id=giveaway.channel_id,
                        giveaway_message_id=giveaway.message_id,
                        claim_end_time=giveaway.claim_end_time,
                    )
                    bot.add_view(view)

        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    @tasks.loop(seconds=30)
    async def check_giveaways(self):
        now = datetime.now(timezone.utc)
        ended_giveaways = []
        expired_claims = []

        for msg_id, giveaway in list(self.giveaway_data.active_giveaways.items()):
            if not giveaway.ended and now >= ensure_aware(giveaway.end_time):
                ended_giveaways.append((msg_id, giveaway))
            elif giveaway.ended and giveaway.claim_end_time and now >= ensure_aware(giveaway.claim_end_time):
                expired_claims.append((msg_id, giveaway))

        for msg_id, giveaway in ended_giveaways:
            await self.end_giveaway(msg_id, giveaway)

        for msg_id, giveaway in expired_claims:
            await self.expire_claim(msg_id, giveaway)

    async def expire_claim(self, message_id: int, giveaway: Giveaway):
        channel = self.bot.get_channel(giveaway.channel_id)
        if channel and giveaway.announcement_message_id:
            try:
                msg = await channel.fetch_message(giveaway.announcement_message_id)
                if msg.embeds:
                    embed = msg.embeds[0]
                    if "expired" not in (embed.description or "").lower():
                        embed.description = (embed.description or "") + "\n\n⏰ Claim period has expired."
                        embed.color = discord.Color.red()
                    await msg.edit(embed=embed, view=None)
            except Exception as e:
                print(f"Failed to expire claim view: {e}")

        self.giveaway_data.remove_giveaway(message_id)

    async def end_giveaway(self, message_id: int, giveaway: Giveaway):
        giveaway.ended = True

        channel = self.bot.get_channel(giveaway.channel_id)
        if not channel:
            print(f"Channel not found for giveaway {message_id}")
            self.giveaway_data.remove_giveaway(message_id)
            return

        try:
            original_msg = await channel.fetch_message(message_id)
        except Exception:
            print(f"Original message not found for giveaway {message_id}")
            self.giveaway_data.remove_giveaway(message_id)
            return

        winners = giveaway.pick_winners()

        results_embed = build_ended_embed(giveaway, winners)
        await original_msg.edit(embed=results_embed, view=None)

        if winners:
            winner_mentions = [f"<@{w}>" for w in winners]

            claim_end_time = datetime.now(timezone.utc) + timedelta(seconds=giveaway.claim_time_seconds)

            claim_view = WinnerClaimView(
                giveaway_data=self.giveaway_data,
                winners=winners,
                prize=giveaway.prize,
                giveaway_channel_id=giveaway.channel_id,
                giveaway_message_id=message_id,
                claim_end_time=claim_end_time,
            )

            announcement_msg = await channel.send(
                content=f"🎉 Congratulations {', '.join(winner_mentions)}, you won **{giveaway.prize}**!",
                view=claim_view,
            )

            giveaway.winners = winners
            giveaway.announcement_message_id = announcement_msg.id
            giveaway.claim_end_time = claim_end_time
            self.giveaway_data.add_giveaway(message_id, giveaway)

        else:
            self.giveaway_data.remove_giveaway(message_id)

    # ------------------------------------------------------------------
    # Slash Commands (NOW USING DASHBOARD AWARE @admin_only())
    # ------------------------------------------------------------------

    giveaway_group = app_commands.Group(name="giveaway", description="Giveaway management commands")

    @giveaway_group.command(name="create", description="Create a new giveaway")
    @app_commands.describe(
        prize="What the winner(s) get (this is also used as the giveaway title)",
        time="How long the giveaway runs, e.g. 1h, 30m, 2d, 45s (combos like 1d12h also work)",
        claim_time="How long the winner has to claim, e.g. 10m, 1h (default 10m)",
        winners="Number of winners (default 1)",
    )
    @admin_only()
    async def create_giveaway(
        self,
        interaction: discord.Interaction,
        prize: str,
        time: str,
        claim_time: str = "10m",
        winners: int = 1,
    ):
        total_seconds = parse_duration(time)
        if total_seconds is None or total_seconds <= 0:
            await interaction.response.send_message(
                "❌ Invalid duration! Use a number plus d/h/m/s, e.g. `1h`, `30m`, `2d`, `45s` (or a combo like `1d12h`).",
                ephemeral=True,
            )
            return

        if winners < 1 or winners > 25:
            await interaction.response.send_message("❌ Winners must be between 1 and 25!", ephemeral=True)
            return

        total_claim_seconds = parse_duration(claim_time)
        if total_claim_seconds is None:
            await interaction.response.send_message(
                "❌ Invalid claim time! Use a number plus d/h/m/s, e.g. `10m`, `1h`, `30s`.",
                ephemeral=True,
            )
            return
        if total_claim_seconds < 30:
            await interaction.response.send_message("❌ Claim time must be at least 30 seconds!", ephemeral=True)
            return

        channel = interaction.channel
        end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)

        giveaway = Giveaway(
            channel_id=channel.id,
            end_time=end_time,
            prize=prize,
            winners_count=winners,
            title=prize,
            description="",
            host_id=interaction.user.id,
            claim_time_seconds=total_claim_seconds,
        )

        embed = build_running_embed(giveaway)

        await interaction.response.send_message("✅ Creating giveaway...", ephemeral=True)

        # 1. Send the message WITHOUT the view first so we can get the real Message ID
        message = await channel.send(embed=embed)

        # 2. Update the giveaway object with the real message ID and save to DB
        giveaway.message_id = message.id
        self.giveaway_data.add_giveaway(message.id, giveaway)

        # 3. Now create the view with the correct custom_id and edit the message to attach it
        view = GiveawayView(self.giveaway_data, giveaway)
        await message.edit(view=view)

        await interaction.edit_original_response(
            content=f"✅ Giveaway created in {channel.mention}!"
        )

    @giveaway_group.command(name="sponsor", description="Become the sponsor responsible for paying out this prize claim")
    async def giveaway_sponsor(self, interaction: discord.Interaction):
        if not is_claim_ticket(interaction.channel):
            await interaction.response.send_message(
                "❌ This command can only be used inside a prize claim ticket.", ephemeral=True
            )
            return

        if not is_staff_user(interaction):
            await interaction.response.send_message(
                "❌ You need the Staff role to sponsor a prize claim.", ephemeral=True
            )
            return

        existing = self.sponsors.get(interaction.channel.id)
        if existing:
            sponsor_id = existing["sponsor_id"]
            if sponsor_id == interaction.user.id:
                await interaction.response.send_message(
                    "❌ You are already the sponsor of this claim!", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ This claim already has a sponsor: <@{sponsor_id}>. "
                    f"They (or an admin) must use `/giveaway unsponsor` before someone else can sponsor it.",
                    ephemeral=True,
                )
            return

        self.sponsors.set(interaction.channel.id, interaction.guild.id, interaction.user.id)

        embed = discord.Embed(
            title="💰 Sponsor Assigned",
            description=f"{interaction.user.mention} will be paying out this prize to the winner.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed)

    @giveaway_group.command(name="unsponsor", description="Remove the current sponsor from this prize claim")
    async def giveaway_unsponsor(self, interaction: discord.Interaction):
        if not is_claim_ticket(interaction.channel):
            await interaction.response.send_message(
                "❌ This command can only be used inside a prize claim ticket.", ephemeral=True
            )
            return

        existing = self.sponsors.get(interaction.channel.id)
        if not existing:
            await interaction.response.send_message(
                "❌ There is no sponsor currently assigned to this claim.", ephemeral=True
            )
            return

        is_current_sponsor = interaction.user.id == existing["sponsor_id"]
        if not (is_current_sponsor or is_admin_user(interaction)):
            await interaction.response.send_message(
                f"❌ Only the assigned sponsor (<@{existing['sponsor_id']}>) or an admin can remove this sponsor.",
                ephemeral=True,
            )
            return

        self.sponsors.remove(interaction.channel.id)

        embed = discord.Embed(
            title="🚫 Sponsor Removed",
            description=(
                f"<@{existing['sponsor_id']}> is no longer the sponsor of this claim.\n"
                f"Another staff member can now use `/giveaway sponsor`."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed)

    @giveaway_group.command(name="paid", description="Mark this prize claim as paid and notify the winner")
    async def giveaway_paid(self, interaction: discord.Interaction):
        if not is_claim_ticket(interaction.channel):
            await interaction.response.send_message(
                "❌ This command can only be used inside a prize claim ticket.", ephemeral=True
            )
            return

        sponsor = self.sponsors.get(interaction.channel.id)
        is_sponsor = sponsor is not None and interaction.user.id == sponsor["sponsor_id"]
        if not (is_sponsor or is_staff_user(interaction)):
            await interaction.response.send_message(
                "❌ Only the assigned sponsor or a user with the Staff role can mark this claim as paid.",
                ephemeral=True,
            )
            return

        winner = get_claim_winner(interaction.channel)
        if winner is None:
            await interaction.response.send_message(
                "❌ Couldn't determine the winner of this claim ticket.", ephemeral=True
            )
            return

        cfg = get_guild_config(interaction.client.db, interaction.guild.id)
        vouch_channel_id = cfg.get("VOUCH_CHANNEL_ID")
        vouch_channel = interaction.guild.get_channel(vouch_channel_id) if vouch_channel_id else None
        vouch_text = vouch_channel.mention if vouch_channel else "our vouch channel"

        message_text = (
            f"🎉 Your giveaway prize has been paid out!\n\n"
            f"If you have a moment, please leave a vouch in {vouch_text} — it really helps us out!"
        )

        await interaction.response.send_message(f"{winner.mention} {message_text}")

        try:
            dm_embed = discord.Embed(
                title="✅ Prize Paid",
                description=message_text,
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            if interaction.guild.icon:
                dm_embed.set_thumbnail(url=interaction.guild.icon.url)
            await winner.send(embed=dm_embed)
        except Exception as e:
            print(f"Failed to DM winner about payment: {e}")

    async def _giveaway_autocomplete(self, current: str) -> List[app_commands.Choice[str]]:
        """Shared autocomplete for /giveaway end, reroll, and delete — lets staff pick
        a giveaway by title instead of having to copy/paste a raw message ID."""
        current = (current or "").lower()
        choices = []
        for msg_id, g in self.giveaway_data.active_giveaways.items():
            status = "Ended" if g.ended else "Active"
            label = f"{g.title} — {status} ({g.prize})"[:100]
            if current in g.title.lower() or current in str(msg_id):
                choices.append(app_commands.Choice(name=label, value=str(msg_id)))
        return choices[:25]

    @giveaway_group.command(name="end", description="Force end a giveaway early")
    @app_commands.describe(message_id="The giveaway to end")
    @admin_only()
    async def end_giveaway_early(self, interaction: discord.Interaction, message_id: str):
        try: msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid message ID!", ephemeral=True)
            return

        if msg_id not in self.giveaway_data.active_giveaways:
            await interaction.response.send_message("❌ Giveaway not found or already ended!", ephemeral=True)
            return

        giveaway = self.giveaway_data.active_giveaways[msg_id]
        if giveaway.ended:
            await interaction.response.send_message("❌ This giveaway has already ended!", ephemeral=True)
            return

        await self.end_giveaway(msg_id, giveaway)
        await interaction.response.send_message("✅ Giveaway ended!", ephemeral=True)

    @end_giveaway_early.autocomplete("message_id")
    async def end_giveaway_early_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_autocomplete(current)

    @giveaway_group.command(name="reroll", description="Reroll a giveaway winner")
    @app_commands.describe(message_id="The giveaway to reroll")
    @admin_only()
    async def reroll_giveaway(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer()

        try: msg_id = int(message_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid message ID!", ephemeral=True)
            return

        if msg_id not in self.giveaway_data.active_giveaways:
            await interaction.followup.send("❌ Giveaway not found! Make sure you are using the **original** giveaway message ID, not the winner announcement ID.", ephemeral=True)
            return

        giveaway = self.giveaway_data.active_giveaways[msg_id]

        if not giveaway.ended:
            await interaction.followup.send("❌ This giveaway hasn't ended yet! Use `/giveaway end` first.", ephemeral=True)
            return

        if giveaway.claimed_users:
            claimers = [f"<@{uid}>" for uid in giveaway.claimed_users]
            await interaction.followup.send(
                f"❌ Cannot reroll! The following users have already opened a claim ticket for this prize: {', '.join(claimers)}",
                ephemeral=True
            )
            return

        eligible_entries = [uid for uid in set(giveaway.entries) if uid not in giveaway.winners]
        
        if not eligible_entries:
            await interaction.followup.send("❌ There are no other entries to reroll from (everyone already won)!", ephemeral=True)
            return

        actual_winners_count = min(giveaway.winners_count, len(eligible_entries))
        new_winners = random.sample(eligible_entries, actual_winners_count)

        channel = self.bot.get_channel(giveaway.channel_id)
        if not channel:
            await interaction.followup.send("❌ Giveaway channel not found.", ephemeral=True)
            return

        if giveaway.announcement_message_id:
            try:
                old_announcement = await channel.fetch_message(giveaway.announcement_message_id)
                await old_announcement.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        giveaway.winners = new_winners
        giveaway.claimed_users = set()
        giveaway.claim_channels = {}
        giveaway.claim_end_time = datetime.now(timezone.utc) + timedelta(seconds=giveaway.claim_time_seconds)
        self.giveaway_data.add_giveaway(msg_id, giveaway)

        winner_mentions = [f"<@{w}>" for w in new_winners]
        announcement_embed = discord.Embed(
            title="🎉 Giveaway Rerolled! 🎉",
            description=(
                f"**Giveaway:** {giveaway.title}\n"
                f"**Prize:** {giveaway.prize}\n\n"
                f"**New Winners:** {', '.join(winner_mentions)}\n\n"
                f"📝 Click the **Claim Prize** button below to claim your prize!\n\n"
                f"⏰ Claim deadline: <t:{int(giveaway.claim_end_time.timestamp())}:R>"
            ),
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc),
        )
        announcement_embed.set_footer(text="Prize claim is open (Rerolled).")

        claim_view = WinnerClaimView(
            giveaway_data=self.giveaway_data,
            winners=new_winners,
            prize=giveaway.prize,
            giveaway_channel_id=giveaway.channel_id,
            giveaway_message_id=msg_id,
            claim_end_time=giveaway.claim_end_time,
        )

        announcement_msg = await channel.send(content=" ".join(winner_mentions), embed=announcement_embed, view=claim_view)

        giveaway.announcement_message_id = announcement_msg.id
        self.giveaway_data.add_giveaway(msg_id, giveaway)

        await interaction.followup.send(f"✅ Giveaway rerolled! New winners: {', '.join(winner_mentions)}")

    @reroll_giveaway.autocomplete("message_id")
    async def reroll_giveaway_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_autocomplete(current)

    @giveaway_group.command(name="delete", description="Permanently delete a giveaway (messages, DB entry, everything)")
    @app_commands.describe(message_id="The giveaway to delete")
    @admin_only()
    async def delete_giveaway(self, interaction: discord.Interaction, message_id: str):
        try: msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid message ID!", ephemeral=True)
            return

        giveaway = self.giveaway_data.active_giveaways.get(msg_id)
        if not giveaway:
            await interaction.response.send_message("❌ Giveaway not found!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(giveaway.channel_id)
        deleted_messages = []

        if channel:
            # Delete the original giveaway message
            try:
                original_msg = await channel.fetch_message(msg_id)
                await original_msg.delete()
                deleted_messages.append("original giveaway message")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

            # Delete the winners/claim announcement message, if one was posted
            if giveaway.announcement_message_id:
                try:
                    announcement_msg = await channel.fetch_message(giveaway.announcement_message_id)
                    await announcement_msg.delete()
                    deleted_messages.append("winner announcement message")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        # Wipe from memory + MongoDB
        self.giveaway_data.remove_giveaway(msg_id)

        summary = f" ({', '.join(deleted_messages)} deleted)" if deleted_messages else ""
        await interaction.followup.send(
            f"✅ Giveaway **{giveaway.title}** has been permanently deleted{summary}.", ephemeral=True
        )

    @delete_giveaway.autocomplete("message_id")
    async def delete_giveaway_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._giveaway_autocomplete(current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))