import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta, timezone
import random
from typing import Optional, List
from cogs.config import admin_only, get_guild_config

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
# Giveaway model
# ---------------------------------------------------------------------------

class Giveaway:
    def __init__(self, channel_id: int, end_time: datetime, prize: str,
                 winners_count: int, title: str, description: str,
                 host_id: int, message_id: int = None, claim_time_seconds: int = 600):
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
        )
        giveaway.entries = data.get('entries', [])
        giveaway.ended = data.get('ended', False)
        giveaway.winners = data.get('winners', [])
        giveaway.announcement_message_id = data.get('announcement_message_id')
        giveaway.claimed_users = set(data.get('claimed_users', []))

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
            label="🎁 Claim Prize",
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

        await self.create_claim_ticket(interaction, interaction.user.id)

    async def create_claim_ticket(self, interaction: discord.Interaction, winner_id: int):
        guild = interaction.guild
        uname = interaction.user.name.lower()

        for channel in guild.text_channels:
            if channel.name == f"claim-{uname}":
                await interaction.response.send_message(
                    f"❌ You already have an open claim ticket: {channel.mention}", ephemeral=True,
                )
                return

        claim_category = discord.utils.get(guild.categories, name="Claim Tickets")
        if not claim_category:
            claim_category = await guild.create_category("Claim Tickets")
            await claim_category.set_permissions(guild.default_role, read_messages=False)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                read_message_history=True, attach_files=True,
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
                f"🔗 **Proof/Original Giveaway:** [Click Here]({giveaway_link})\n\n"
                f"Please wait for staff to process your claim.\n\n"
                f"━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Category",   value="Prize Claim",            inline=True)
        embed.set_footer(text=f"Channel ID: {ticket.id}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        from cogs.tickets_base import TicketView
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
            self.giveaway_data.add_giveaway(self.giveaway_message_id, giveaway_obj)

        await interaction.response.send_message(
            f"✅ Claim ticket created: {ticket.mention}", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Giveaway Enter Button / View
# ---------------------------------------------------------------------------

class GiveawayButton(discord.ui.Button):
    def __init__(self, giveaway_data: GiveawayData, giveaway: Giveaway):
        super().__init__(
            label="🎉 Enter Giveaway",
            style=discord.ButtonStyle.primary,
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
                    if msg.embeds:
                        embed = msg.embeds[0]
                        fields = [(f.name, f.value, f.inline) for f in embed.fields]
                        embed.clear_fields()
                        for name, value, inline in fields:
                            if name == "📊 Total Entries":
                                embed.add_field(name="📊 Total Entries", value=str(len(self.giveaway.entries)), inline=inline)
                            else:
                                embed.add_field(name=name, value=value, inline=inline)
                        await msg.edit(embed=embed)
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

        results_embed = discord.Embed(
            title=f"🎉 {giveaway.title} - GIVEAWAY ENDED 🎉",
            description=f"{giveaway.description}\n\n**Prize:** {giveaway.prize}",
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc),
        )

        if winners:
            winner_mentions = [f"<@{w}>" for w in winners]
            results_embed.add_field(name=f"🏆 Winners ({len(winners)})", value=", ".join(winner_mentions), inline=False)
            await original_msg.edit(embed=results_embed, view=None)

            claim_end_time = datetime.now(timezone.utc) + timedelta(seconds=giveaway.claim_time_seconds)

            announcement_embed = discord.Embed(
                title="🎉 Giveaway Winners Announced! 🎉",
                description=(
                    f"**Giveaway:** {giveaway.title}\n"
                    f"**Prize:** {giveaway.prize}\n\n"
                    f"**Winners:** {', '.join(winner_mentions)}\n\n"
                    f"📝 Click the **Claim Prize** button below to claim your prize!\n\n"
                    f"⏰ Claim deadline: <t:{int(claim_end_time.timestamp())}:R>"
                ),
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            announcement_embed.set_footer(text="Prize claim is open.")

            claim_view = WinnerClaimView(
                giveaway_data=self.giveaway_data,
                winners=winners,
                prize=giveaway.prize,
                giveaway_channel_id=giveaway.channel_id,
                giveaway_message_id=message_id,
                claim_end_time=claim_end_time,
            )

            announcement_msg = await channel.send(content=" ".join(winner_mentions), embed=announcement_embed, view=claim_view)

            giveaway.winners = winners
            giveaway.announcement_message_id = announcement_msg.id
            giveaway.claim_end_time = claim_end_time
            self.giveaway_data.add_giveaway(message_id, giveaway)

            for winner_id in winners:
                try:
                    user = await self.bot.fetch_user(winner_id)
                    dm_embed = discord.Embed(
                        title="🎉 Congratulations! You won a giveaway! 🎉",
                        description=(
                            f"You won **{giveaway.prize}** in **{giveaway.title}**!\n\n"
                            f"Please go to {channel.mention} and click the "
                            f"**Claim Prize** button to claim your prize.\n\n"
                            f"⏰ Claim deadline: <t:{int(claim_end_time.timestamp())}:R>"
                        ),
                        color=0x00FF00,
                    )
                    await user.send(embed=dm_embed)
                except Exception: pass

        else:
            results_embed.add_field(name="❌ No Winners", value="No one entered this giveaway!", inline=False)
            await original_msg.edit(embed=results_embed, view=None)
            self.giveaway_data.remove_giveaway(message_id)

    # ------------------------------------------------------------------
    # Slash Commands (NOW USING DASHBOARD AWARE @admin_only())
    # ------------------------------------------------------------------

    @app_commands.command(name="giveaway", description="Create a new giveaway")
    @app_commands.describe(
        channel="Channel to post the giveaway", title="Title of the giveaway", description="Description of the giveaway",
        prize="What users can win", winners="Number of winners", duration_minutes="Minutes until giveaway ends",
        duration_seconds="Seconds until giveaway ends", claim_time_minutes="Minutes winners have to claim their prize (default 10)",
        claim_time_seconds="Extra seconds for claim time (default 0)",
    )
    @admin_only()
    async def create_giveaway(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        description: str,
        prize: str,
        winners: int = 1,
        duration_minutes: int = 0,
        duration_seconds: int = 0,
        claim_time_minutes: int = 10,
        claim_time_seconds: int = 0,
    ):
        total_seconds = (duration_minutes * 60) + duration_seconds
        if total_seconds <= 0:
            await interaction.response.send_message("❌ Please specify a duration greater than 0!", ephemeral=True)
            return

        if winners < 1 or winners > 25:
            await interaction.response.send_message("❌ Winners must be between 1 and 25!", ephemeral=True)
            return

        total_claim_seconds = (claim_time_minutes * 60) + claim_time_seconds
        if total_claim_seconds < 30:
            await interaction.response.send_message("❌ Claim time must be at least 30 seconds!", ephemeral=True)
            return

        end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
        end_timestamp = int(end_time.timestamp())

        embed = discord.Embed(title=f"🎉 {title} 🎉", description=description, color=0x00FF00, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="🎁 Prize", value=prize, inline=False)
        embed.add_field(name="👑 Winners", value=str(winners), inline=True)
        embed.add_field(name="⏰ Ends", value=f"<t:{end_timestamp}:F> (<t:{end_timestamp}:R>)", inline=True)
        embed.add_field(name="📊 Total Entries", value="0", inline=False)
        embed.set_footer(text=f"Hosted by {interaction.user.name}", icon_url=interaction.user.display_avatar.url)

        giveaway = Giveaway(channel_id=channel.id, end_time=end_time, prize=prize, winners_count=winners, title=title,
                            description=description, host_id=interaction.user.id, claim_time_seconds=total_claim_seconds)

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

    @app_commands.command(name="endgiveaway", description="Force end a giveaway early")
    @app_commands.describe(message_id="The message ID of the original giveaway")
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

    @app_commands.command(name="reroll", description="Reroll a giveaway winner")
    @app_commands.describe(message_id="The message ID of the ORIGINAL giveaway")
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
            await interaction.followup.send("❌ This giveaway hasn't ended yet! Use `/endgiveaway` first.", ephemeral=True)
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

        for winner_id in new_winners:
            try:
                user = await self.bot.fetch_user(winner_id)
                dm_embed = discord.Embed(
                    title="🎉 Congratulations! You won a rerolled giveaway! 🎉",
                    description=(
                        f"You won **{giveaway.prize}** in **{giveaway.title}**!\n\n"
                        f"Please go to {channel.mention} and click the **Claim Prize** button to claim your prize.\n\n"
                        f"⏰ Claim deadline: <t:{int(giveaway.claim_end_time.timestamp())}:R>"
                    ),
                    color=0x00FF00,
                )
                await user.send(embed=dm_embed)
            except Exception: pass

        await interaction.followup.send(f"✅ Giveaway rerolled! New winners: {', '.join(winner_mentions)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))