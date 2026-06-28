import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from cogs.config import mod_only, get_guild_config
# ---------------------------------------------------------------------------
# Persistence for Warnings (MongoDB)
# ---------------------------------------------------------------------------

# In-memory warning store  { guild_id: { user_id: [ {reason, mod, ts}, ... ] } }
_warnings: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))

def load_warnings(db):
    global _warnings
    try:
        collection = db["warnings"]
        for doc in collection.find():
            guild_id = doc["guild_id"]
            user_id = doc["user_id"]
            _warnings[guild_id][user_id] = doc["warnings"]
        print(f"✅ Loaded warnings from MongoDB")
    except Exception as e:
        print(f"❌ Failed to load warnings: {e}")

def save_user_warnings(db, guild_id: int, user_id: int, warnings_list: list):
    try:
        collection = db["warnings"]
        collection.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$set": {"warnings": warnings_list}},
            upsert=True
        )
    except Exception as e:
        print(f"❌ Failed to save warnings to DB: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _log(interaction: discord.Interaction, embed: discord.Embed):
    """Send an embed to the mod-log channel if it exists."""
    # Fetch the dynamic config from the website dashboard
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    log_channel_id = cfg.get("LOG_CHANNEL_ID")
    
    if log_channel_id:
        ch = interaction.guild.get_channel(log_channel_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass


def _mod_embed(
    action: str,
    target: discord.Member | discord.User,
    mod: discord.Member,
    reason: str,
    color: int,
    extra: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔨 {action}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User",       value=f"{target.mention} (`{target}`)",  inline=True)
    embed.add_field(name="Moderator",  value=f"{mod.mention}",                  inline=True)
    embed.add_field(name="Reason",     value=reason or "No reason provided.",   inline=False)
    if extra:
        embed.add_field(name="Details", value=extra, inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"User ID: {target.id}")
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Load warnings from MongoDB when the cog starts
        load_warnings(self.bot.db)

    # Global error handler for this cog
    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        msg = str(error) if isinstance(error, app_commands.CheckFailure) else f"❌ Error: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /mute  (Discord timeout)
    # ------------------------------------------------------------------

    @app_commands.command(name="mute", description="Timeout a member")
    @app_commands.describe(
        member="Member to mute",
        duration="Duration in minutes (default 10)",
        reason="Reason for the mute",
    )
    @mod_only()
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: int = 10,
        reason: str = "No reason provided.",
    ):
        if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't mute someone with an equal or higher role.", ephemeral=True)
            return
        if member.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't mute an administrator.", ephemeral=True)
            return

        until = datetime.now(timezone.utc) + timedelta(minutes=duration)
        try:
            await member.timeout(until, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to timeout that member.", ephemeral=True)
            return

        embed = _mod_embed("Mute", member, interaction.user, reason, 0xf39c12,
                           extra=f"Duration: {duration} minute(s)")
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

        try:
            await member.send(
                f"🔇 You have been muted in **{interaction.guild.name}** for **{duration} minute(s)**.\n"
                f"Reason: {reason}"
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /unmute
    # ------------------------------------------------------------------

    @app_commands.command(name="unmute", description="Remove a timeout from a member")
    @app_commands.describe(member="Member to unmute", reason="Reason")
    @mod_only()
    async def unmute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
    ):
        if not member.is_timed_out():
            await interaction.response.send_message("❌ That member is not currently muted.", ephemeral=True)
            return

        try:
            await member.timeout(None, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to remove that timeout.", ephemeral=True)
            return

        embed = _mod_embed("Unmute", member, interaction.user, reason, 0x2ecc71)
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

        try:
            await member.send(f"🔊 Your mute in **{interaction.guild.name}** has been removed.\nReason: {reason}")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /kick
    # ------------------------------------------------------------------

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason")
    @mod_only()
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
    ):
        if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't kick someone with an equal or higher role.", ephemeral=True)
            return

        try:
            await member.send(f"👢 You have been kicked from **{interaction.guild.name}**.\nReason: {reason}")
        except discord.HTTPException:
            pass

        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick that member.", ephemeral=True)
            return

        embed = _mod_embed("Kick", member, interaction.user, reason, 0xe67e22)
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

    # ------------------------------------------------------------------
    # /ban
    # ------------------------------------------------------------------

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Member to ban",
        reason="Reason",
        delete_days="Days of messages to delete (0-7, default 0)",
    )
    @mod_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
        delete_days: int = 0,
    ):
        if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't ban someone with an equal or higher role.", ephemeral=True)
            return

        delete_days = max(0, min(7, delete_days))

        try:
            await member.send(f"🔨 You have been banned from **{interaction.guild.name}**.\nReason: {reason}")
        except discord.HTTPException:
            pass

        try:
            await member.ban(reason=reason, delete_message_days=delete_days)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban that member.", ephemeral=True)
            return

        embed = _mod_embed("Ban", member, interaction.user, reason, 0xe74c3c,
                           extra=f"Messages deleted: {delete_days} day(s)")
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

    # ------------------------------------------------------------------
    # /unban
    # ------------------------------------------------------------------

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="The user's Discord ID", reason="Reason")
    @mod_only()
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason provided.",
    ):
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID — must be a number.", ephemeral=True)
            return

        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            await interaction.response.send_message("❌ That user is not banned.", ephemeral=True)
            return

        try:
            await interaction.guild.unban(ban_entry.user, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unban users.", ephemeral=True)
            return

        embed = _mod_embed("Unban", ban_entry.user, interaction.user, reason, 0x2ecc71)
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

    # ------------------------------------------------------------------
    # /warn  (SAVES TO MONGODB)
    # ------------------------------------------------------------------

    @app_commands.command(name="warn", description="Issue a warning to a member")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @mod_only()
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
    ):
        entry = {
            "reason": reason,
            "mod":    str(interaction.user),
            "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        _warnings[interaction.guild.id][member.id].append(entry)
        save_user_warnings(self.bot.db, interaction.guild.id, member.id, _warnings[interaction.guild.id][member.id])
        
        count = len(_warnings[interaction.guild.id][member.id])

        embed = _mod_embed("Warning", member, interaction.user, reason, 0xf1c40f,
                           extra=f"Total warnings: **{count}**")
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

        try:
            await member.send(
                f"⚠️ You have received a warning in **{interaction.guild.name}**.\n"
                f"Reason: {reason}\n"
                f"You now have **{count}** warning(s)."
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # /warnings
    # ------------------------------------------------------------------

    @app_commands.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    @mod_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        entries = _warnings[interaction.guild.id][member.id]

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member}",
            color=0xf1c40f,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not entries:
            embed.description = "✅ This member has no warnings."
        else:
            for i, w in enumerate(entries, 1):
                embed.add_field(
                    name=f"Warning #{i} — {w['ts']}",
                    value=f"**Reason:** {w['reason']}\n**By:** {w['mod']}",
                    inline=False,
                )
            embed.set_footer(text=f"Total: {len(entries)} warning(s) | User ID: {member.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /clearwarnings  (SAVES TO MONGODB)
    # ------------------------------------------------------------------

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @mod_only()
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        count = len(_warnings[interaction.guild.id][member.id])
        if count == 0:
            await interaction.response.send_message(f"✅ {member.mention} has no warnings to clear.", ephemeral=True)
            return

        _warnings[interaction.guild.id][member.id].clear()
        save_user_warnings(self.bot.db, interaction.guild.id, member.id, [])

        embed = discord.Embed(
            title="🗑️ Warnings Cleared",
            description=f"Cleared **{count}** warning(s) for {member.mention}.",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Cleared by {interaction.user} | User ID: {member.id}")
        await interaction.response.send_message(embed=embed)
        await _log(interaction, embed)

    # ------------------------------------------------------------------
    # /purge
    # ------------------------------------------------------------------

    @app_commands.command(name="purge", description="Bulk delete messages in this channel")
    @app_commands.describe(
        amount="Number of messages to delete (1–100)",
        member="Only delete messages from this member (optional)",
    )
    @mod_only()
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        member: discord.Member = None,
    ):
        amount = max(1, min(100, amount))
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message):
            return member is None or m.author == member

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to delete messages here.", ephemeral=True)
            return

        target_str = f" from {member.mention}" if member else ""
        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** message(s){target_str}.", ephemeral=True
        )

        embed = discord.Embed(
            title="🗑️ Purge",
            description=f"**{len(deleted)}** message(s) deleted{target_str} in {interaction.channel.mention}.",
            color=0x3498db,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"By {interaction.user}")
        await _log(interaction, embed)


    # ------------------------------------------------------------------
    # /lock  (FIXED PERMISSIONS)
    # ------------------------------------------------------------------

    @app_commands.command(name="lock", description="Lock the channel so only Staff can send messages")
    @app_commands.describe(reason="Reason for locking")
    @mod_only()
    async def lock(self, interaction: discord.Interaction, reason: str = "No reason provided."):
        channel = interaction.channel
        cfg = get_guild_config(interaction.client.db, interaction.guild.id)
        staff_role_name = cfg["STAFF_ROLE"]
        staff_role = discord.utils.get(interaction.guild.roles, name=staff_role_name)
        everyone = interaction.guild.default_role

        # Safely check current overwrite status
        overwrite = channel.overwrites_for(everyone)
        if overwrite.send_messages is False:
            await interaction.response.send_message("❌ This channel is already locked.", ephemeral=True)
            return

        if not staff_role:
            await interaction.response.send_message(f"❌ Staff role '{staff_role_name}' not found. Please create it first.", ephemeral=True)
            return

        try:
            # Safely update only send_messages for @everyone
            overwrite.send_messages = False
            await channel.set_permissions(everyone, overwrite=overwrite)
            
            # Ensure Staff can still send
            staff_overwrite = channel.overwrites_for(staff_role)
            staff_overwrite.send_messages = True
            staff_overwrite.read_messages = True
            await channel.set_permissions(staff_role, overwrite=staff_overwrite)
            
            embed = discord.Embed(
                title="🔒 Channel Locked",
                description=f"{channel.mention} has been locked.",
                color=0xe74c3c,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=True)
            embed.set_footer(text=f"Channel ID: {channel.id}")

            await interaction.response.send_message(embed=embed)
            await _log(interaction, embed)
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to edit this channel.", ephemeral=True)

    # ------------------------------------------------------------------
    # /unlock  (FIXED PERMISSIONS)
    # ------------------------------------------------------------------

    @app_commands.command(name="unlock", description="Unlock the channel so everyone can send messages again")
    @app_commands.describe(reason="Reason for unlocking")
    @mod_only()
    async def unlock(self, interaction: discord.Interaction, reason: str = "No reason provided."):
        channel = interaction.channel
        everyone = interaction.guild.default_role

        # Safely check current overwrite status
        overwrite = channel.overwrites_for(everyone)
        if overwrite.send_messages is not False:
            await interaction.response.send_message("❌ This channel is not locked.", ephemeral=True)
            return

        try:
            # Restore send_messages to default (None) without wiping other overwrites
            overwrite.send_messages = None
            await channel.set_permissions(everyone, overwrite=overwrite)
            
            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=f"{channel.mention} has been unlocked.",
                color=0x2ecc71,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=True)
            embed.set_footer(text=f"Channel ID: {channel.id}")

            await interaction.response.send_message(embed=embed)
            await _log(interaction, embed)
            
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to edit this channel.", ephemeral=True)

    # ------------------------------------------------------------------
    # /rename
    # ------------------------------------------------------------------

    @app_commands.command(name="rename", description="Rename the current channel")
    @app_commands.describe(new_name="New channel name (spaces become dashes)")
    @mod_only()
    async def rename(self, interaction: discord.Interaction, new_name: str):
        clean = new_name.lower().replace(" ", "-")[:50]
        try:
            await interaction.channel.edit(name=clean)
            await interaction.response.send_message(f"✅ Channel renamed to `{clean}`.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to rename this channel.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Rename failed: {e}", ephemeral=True)

    # ------------------------------------------------------------------
    # /add
    # ------------------------------------------------------------------

    @app_commands.command(name="add", description="Add a user to the current channel")
    @app_commands.describe(member="The member to add")
    @mod_only()
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.channel.set_permissions(
            member, read_messages=True, send_messages=True,
            read_message_history=True, attach_files=True,
        )
        await interaction.response.send_message(f"✅ {member.mention} has been added to this channel.")

    # ------------------------------------------------------------------
    # /remove
    # ------------------------------------------------------------------

    @app_commands.command(name="remove", description="Remove a user from the current channel")
    @app_commands.describe(member="The member to remove")
    @mod_only()
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        if member == interaction.user:
            await interaction.response.send_message("❌ You can't remove yourself!", ephemeral=True)
            return
        await interaction.channel.set_permissions(member, read_messages=False, send_messages=False)
        await interaction.response.send_message(f"✅ {member.mention} has been removed from this channel.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))