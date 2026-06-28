import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from cogs.config import mod_only, admin_only, staff_only, get_guild_config
import asyncio
import re
import requests

class StaffUtils(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Advanced Moderation
    # ------------------------------------------------------------------

    @app_commands.command(name="slowmode", description="Set slowmode in current channel (seconds)")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)")
    @mod_only()
    async def slowmode(self, interaction: discord.Interaction, seconds: int):
        if seconds < 0 or seconds > 21600:
            await interaction.response.send_message("❌ Slowmode must be between 0 and 21600 seconds (6 hours).", ephemeral=True)
            return
        
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message(f"✅ Slowmode disabled in {interaction.channel.mention}.")
        else:
            await interaction.response.send_message(f"✅ Slowmode set to {seconds} seconds in {interaction.channel.mention}.")

    @app_commands.command(name="voicekick", description="Kick a member from a voice channel")
    @app_commands.describe(member="Member to kick from voice", reason="Reason for kick")
    @mod_only()
    async def voicekick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("❌ That user is not in a voice channel.", ephemeral=True)
            return
        
        try:
            await member.move_to(None, reason=reason)
            embed = discord.Embed(
                title="🔊 Voice Kick",
                description=f"{member.mention} was kicked from voice channel.",
                color=0xe67e22,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=True)
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to move that member.", ephemeral=True)

    @app_commands.command(name="vcmute", description="Mute a member in voice chat")
    @app_commands.describe(member="Member to mute", reason="Reason")
    @mod_only()
    async def vcmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
        if not member.voice:
            await interaction.response.send_message("❌ That user is not in a voice channel.", ephemeral=True)
            return
        
        try:
            await member.edit(mute=True, reason=reason)
            await interaction.response.send_message(f"✅ {member.mention} has been muted in voice chat.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to mute that member.", ephemeral=True)

    @app_commands.command(name="vcunmute", description="Unmute a member in voice chat")
    @app_commands.describe(member="Member to unmute", reason="Reason")
    @mod_only()
    async def vcunmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
        if not member.voice:
            await interaction.response.send_message("❌ That user is not in a voice channel.", ephemeral=True)
            return
        
        try:
            await member.edit(mute=False, reason=reason)
            await interaction.response.send_message(f"✅ {member.mention} has been unmuted in voice chat.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unmute that member.", ephemeral=True)

    @app_commands.command(name="nickname", description="Change a user's nickname")
    @app_commands.describe(member="Member to rename", nickname="New nickname (leave empty to reset)")
    @mod_only()
    async def nickname(self, interaction: discord.Interaction, member: discord.Member, nickname: Optional[str] = None):
        if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't change the nickname of someone with an equal or higher role.", ephemeral=True)
            return
        
        try:
            await member.edit(nick=nickname)
            if nickname:
                await interaction.response.send_message(f"✅ Changed {member.mention}'s nickname to **{nickname}**.")
            else:
                await interaction.response.send_message(f"✅ Reset {member.mention}'s nickname.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to change that member's nickname.", ephemeral=True)

    @app_commands.command(name="moveall", description="Move all members from one voice channel to another")
    @app_commands.describe(from_channel="Source voice channel", to_channel="Destination voice channel")
    @admin_only()
    async def moveall(self, interaction: discord.Interaction, from_channel: discord.VoiceChannel, to_channel: discord.VoiceChannel):
        members = from_channel.members
        if not members:
            await interaction.response.send_message(f"❌ {from_channel.mention} is empty.", ephemeral=True)
            return
        
        count = 0
        for member in members:
            try:
                await member.move_to(to_channel)
                count += 1
                await asyncio.sleep(0.5)  # Rate limit protection
            except:
                pass
        
        await interaction.response.send_message(f"✅ Moved {count}/{len(members)} members from {from_channel.mention} to {to_channel.mention}.")

    @app_commands.command(name="hide", description="Hide current channel from @everyone")
    @app_commands.describe(reason="Reason for hiding")
    @mod_only()
    async def hide(self, interaction: discord.Interaction, reason: str = "No reason provided."):
        channel = interaction.channel
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        
        if overwrite.read_messages is False:
            await interaction.response.send_message("❌ This channel is already hidden.", ephemeral=True)
            return
        
        overwrite.read_messages = False
        await channel.set_permissions(everyone, overwrite=overwrite)
        
        embed = discord.Embed(
            title="👁️ Channel Hidden",
            description=f"{channel.mention} has been hidden from @everyone.",
            color=0xe74c3c,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reveal", description="Reveal hidden channel to @everyone")
    @app_commands.describe(reason="Reason for revealing")
    @mod_only()
    async def reveal(self, interaction: discord.Interaction, reason: str = "No reason provided."):
        channel = interaction.channel
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        
        if overwrite.read_messages is not False:
            await interaction.response.send_message("❌ This channel is not hidden.", ephemeral=True)
            return
        
        overwrite.read_messages = None
        await channel.set_permissions(everyone, overwrite=overwrite)
        
        embed = discord.Embed(
            title="👁️ Channel Revealed",
            description=f"{channel.mention} is now visible to @everyone.",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=True)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Staff Utility Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="whois", description="Get detailed information about a user")
    @app_commands.describe(member="Member to get info about")
    @staff_only()
    async def whois(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        
        # Get warning count
        warnings = self.bot.db["warnings"].find_one({"guild_id": interaction.guild.id, "user_id": target.id})
        warning_count = len(warnings.get("warnings", [])) if warnings else 0
        
        embed = discord.Embed(
            title=f"👤 User Info: {target}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="ID", value=target.id, inline=True)
        embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Joined Discord", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Nickname", value=target.nick or "None", inline=True)
        embed.add_field(name="Roles", value=", ".join([r.mention for r in target.roles[1:]]) or "None", inline=False)
        embed.add_field(name="Permissions", value=f"Administrator: {target.guild_permissions.administrator}", inline=True)
        embed.add_field(name="Warnings", value=str(warning_count), inline=True)
        embed.add_field(name="Boost Status", value="Boosting" if target.premium_since else "Not Boosting", inline=True)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Get detailed server information")
    @staff_only()
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        
        # Count members by status
        online = sum(1 for m in guild.members if m.status != discord.Status.offline and not m.bot)
        total_members = guild.member_count
        bots = sum(1 for m in guild.members if m.bot)
        humans = total_members - bots
        
        # Count channels
        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        categories = len(guild.categories)
        
        # Features
        features = ", ".join([f.replace("_", " ").title() for f in guild.features[:5]])
        
        embed = discord.Embed(
            title=f"📊 Server Info: {guild.name}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="ID", value=guild.id, inline=True)
        embed.add_field(name="Members", value=f"Total: {total_members}\n👤 Humans: {humans}\n🤖 Bots: {bots}\n🟢 Online: {online}", inline=True)
        embed.add_field(name="Channels", value=f"💬 Text: {text_channels}\n🔊 Voice: {voice_channels}\n📁 Categories: {categories}", inline=True)
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier} | {guild.premium_subscription_count} boosts", inline=True)
        embed.add_field(name="Features", value=features or "None", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="roleinfo", description="Get information about a role")
    @app_commands.describe(role="Role to get info about")
    @staff_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        members_with_role = sum(1 for m in interaction.guild.members if role in m.roles)
        
        embed = discord.Embed(
            title=f"📋 Role Info: {role.name}",
            color=role.color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="ID", value=role.id, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="Position", value=role.position, inline=True)
        embed.add_field(name="Members", value=members_with_role, inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Permissions", value=", ".join([p[0] for p in role.permissions if p[1]])[:1024] or "None", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="channelinfo", description="Get information about the current channel")
    @staff_only()
    async def channelinfo(self, interaction: discord.Interaction):
        channel = interaction.channel
        
        embed = discord.Embed(
            title=f"📺 Channel Info: #{channel.name}",
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="ID", value=channel.id, inline=True)
        embed.add_field(name="Type", value=str(channel.type).title(), inline=True)
        embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(channel.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Position", value=channel.position, inline=True)
        
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="Topic", value=channel.topic or "None", inline=False)
            embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay} seconds", inline=True)
            embed.add_field(name="NSFW", value="Yes" if channel.is_nsfw() else "No", inline=True)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Get a user's avatar")
    @app_commands.describe(member="Member to get avatar of")
    @staff_only()
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        
        embed = discord.Embed(
            title=f"🖼️ Avatar - {target.name}",
            color=discord.Color.blue()
        )
        embed.set_image(url=target.display_avatar.url)
        embed.add_field(name="Download", value=f"[PNG]({target.display_avatar.url}) | [JPG]({target.display_avatar.url.replace('webp', 'png')})", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="servericon", description="Get the server icon")
    @staff_only()
    async def servericon(self, interaction: discord.Interaction):
        guild = interaction.guild
        
        if not guild.icon:
            await interaction.response.send_message("❌ This server doesn't have an icon.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🖼️ Server Icon - {guild.name}",
            color=discord.Color.blue()
        )
        embed.set_image(url=guild.icon.url)
        
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Mass Moderation
    # ------------------------------------------------------------------

    @app_commands.command(name="softban", description="Ban and immediately unban to clear messages")
    @app_commands.describe(member="Member to softban", reason="Reason", delete_days="Days of messages to delete (1-7)")
    @mod_only()
    async def softban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided.", delete_days: int = 7):
        if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't softban someone with an equal or higher role.", ephemeral=True)
            return
        
        delete_days = max(1, min(7, delete_days))
        
        try:
            await member.send(f"🔨 You have been softbanned from **{interaction.guild.name}**.\nReason: {reason}\n*Softban removes your messages but allows you to rejoin.*")
        except:
            pass
        
        await member.ban(reason=f"Softban: {reason}", delete_message_days=delete_days)
        await member.unban(reason=f"Softban expired: {reason}")
        
        embed = discord.Embed(
            title="🔨 Softban",
            description=f"{member.mention} has been softbanned.",
            color=0xe74c3c,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=True)
        embed.add_field(name="Messages Deleted", value=f"{delete_days} days", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="multiban", description="Ban multiple users at once")
    @app_commands.describe(users="Comma-separated user IDs or mentions", reason="Reason")
    @admin_only()
    async def multiban(self, interaction: discord.Interaction, users: str, reason: str = "No reason provided."):
        # Parse user IDs from input
        user_ids = []
        for part in users.split(","):
            part = part.strip()
            # Remove <@> or <@!> formatting if present
            if part.startswith("<@") and part.endswith(">"):
                part = part.replace("<@", "").replace(">", "").replace("!", "")
            if part.isdigit():
                user_ids.append(int(part))
        
        if not user_ids:
            await interaction.response.send_message("❌ No valid user IDs found. Use comma-separated IDs or mentions.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        success = []
        failed = []
        
        for uid in user_ids:
            user = interaction.guild.get_member(uid)
            if not user:
                # Try to ban by ID even if not in server
                try:
                    await interaction.guild.ban(discord.Object(id=uid), reason=reason)
                    success.append(str(uid))
                except:
                    failed.append(str(uid))
            else:
                if user.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
                    failed.append(f"{user.name} (role too high)")
                else:
                    try:
                        await user.ban(reason=reason)
                        success.append(user.name)
                    except:
                        failed.append(user.name)
        
        await interaction.followup.send(
            f"✅ Banned: {len(success)} users\n❌ Failed: {len(failed)} users\n\nSuccess: {', '.join(success) if success else 'None'}\nFailed: {', '.join(failed) if failed else 'None'}"
        )

    @app_commands.command(name="multimute", description="Timeout multiple users at once")
    @app_commands.describe(users="Comma-separated user mentions", duration_minutes="Duration in minutes", reason="Reason")
    @admin_only()
    async def multimute(self, interaction: discord.Interaction, users: str, duration_minutes: int = 10, reason: str = "No reason provided."):
        # Parse user mentions
        user_ids = []
        for part in users.split(","):
            part = part.strip()
            if part.startswith("<@") and part.endswith(">"):
                part = part.replace("<@", "").replace(">", "").replace("!", "")
                if part.isdigit():
                    user_ids.append(int(part))
        
        if not user_ids:
            await interaction.response.send_message("❌ No valid user mentions found.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        success = []
        failed = []
        until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        
        for uid in user_ids:
            member = interaction.guild.get_member(uid)
            if not member:
                failed.append(str(uid))
                continue
            
            if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
                failed.append(member.name)
                continue
            
            try:
                await member.timeout(until, reason=reason)
                success.append(member.name)
            except:
                failed.append(member.name)
        
        await interaction.followup.send(
            f"✅ Muted: {len(success)} users for {duration_minutes} minutes\n❌ Failed: {len(failed)} users\n\nSuccess: {', '.join(success) if success else 'None'}"
        )

    # ------------------------------------------------------------------
    # Logging & Investigation
    # ------------------------------------------------------------------

    @app_commands.command(name="userwarnings", description="View all warnings for a user (paginated)")
    @app_commands.describe(member="Member to view warnings for", page="Page number")
    @mod_only()
    async def userwarnings(self, interaction: discord.Interaction, member: discord.Member, page: int = 1):
        warnings_data = self.bot.db["warnings"].find_one({"guild_id": interaction.guild.id, "user_id": member.id})
        warnings = warnings_data.get("warnings", []) if warnings_data else []
        
        if not warnings:
            await interaction.response.send_message(f"✅ {member.mention} has no warnings.", ephemeral=True)
            return
        
        # Pagination
        items_per_page = 5
        total_pages = (len(warnings) + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start = (page - 1) * items_per_page
        end = start + items_per_page
        
        embed = discord.Embed(
            title=f"⚠️ Warnings for {member.name}",
            color=0xf1c40f,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        for i, w in enumerate(warnings[start:end], start + 1):
            embed.add_field(
                name=f"Warning #{i} - {w.get('ts', 'Unknown date')}",
                value=f"**Reason:** {w.get('reason', 'No reason')}\n**By:** {w.get('mod', 'Unknown')}",
                inline=False
            )
        
        embed.set_footer(text=f"Page {page}/{total_pages} | Total: {len(warnings)} warnings")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="modlogs", description="View recent moderation actions (coming soon)")
    @mod_only()
    async def modlogs(self, interaction: discord.Interaction):
        # This would require storing moderation actions in DB
        await interaction.response.send_message("📝 Modlogs feature coming soon! This will track all moderation actions in the server.", ephemeral=True)

    @app_commands.command(name="audit", description="Check recent audit log entries")
    @app_commands.describe(action="Filter by action type", limit="Number of entries (1-100)")
    @admin_only()
    async def audit(self, interaction: discord.Interaction, action: Optional[str] = None, limit: int = 10):
        limit = max(1, min(100, limit))
        
        try:
            async for entry in interaction.guild.audit_logs(limit=limit, action=getattr(discord.AuditLogAction, action.upper(), None) if action else None):
                embed = discord.Embed(
                    title=f"📋 Audit Log: {entry.action.name}",
                    color=discord.Color.blue(),
                    timestamp=entry.created_at
                )
                embed.add_field(name="User", value=entry.user.mention if entry.user else "Unknown", inline=True)
                embed.add_field(name="Target", value=str(entry.target) if entry.target else "Unknown", inline=True)
                if entry.reason:
                    embed.add_field(name="Reason", value=entry.reason, inline=False)
                
                # Wait for response
                await interaction.response.send_message(embed=embed)
                return
        except:
            pass
        
        await interaction.response.send_message("❌ Could not fetch audit logs. Make sure I have 'View Audit Log' permission.", ephemeral=True)

    @app_commands.command(name="rolemembers", description="List all members with a specific role")
    @app_commands.describe(role="The role to list members for")
    @staff_only()
    async def rolemembers(self, interaction: discord.Interaction, role: discord.Role):
        members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
        
        if not members:
            await interaction.response.send_message(f"❌ No members found with {role.mention}.", ephemeral=True)
            return
        
        member_list = ", ".join([m.mention for m in members[:20]])
        embed = discord.Embed(
            title=f"👥 Members with {role.name}",
            description=member_list,
            color=role.color
        )
        
        if len(members) > 20:
            embed.set_footer(text=f"Showing 20 of {len(members)} members")
        
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Server Management
    # ------------------------------------------------------------------

    @app_commands.command(name="cleanup", description="Delete bot messages in current channel")
    @app_commands.describe(limit="Number of messages to check (1-100)")
    @mod_only()
    async def cleanup(self, interaction: discord.Interaction, limit: int = 50):
        limit = max(1, min(100, limit))
        
        await interaction.response.defer(ephemeral=True)
        
        deleted = 0
        async for msg in interaction.channel.history(limit=limit):
            if msg.author == self.bot.user:
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.5)
                except:
                    pass
        
        await interaction.followup.send(f"✅ Deleted {deleted} bot message(s).", ephemeral=True)

    @app_commands.command(name="clonechannel", description="Clone the current channel with all permissions")
    @app_commands.describe(name="Name for the new channel")
    @admin_only()
    async def clonechannel(self, interaction: discord.Interaction, name: Optional[str] = None):
        channel = interaction.channel
        new_name = name or f"{channel.name}-clone"
        
        try:
            new_channel = await channel.clone(name=new_name)
            await interaction.response.send_message(f"✅ Channel cloned: {new_channel.mention}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to clone this channel.", ephemeral=True)

    @app_commands.command(name="addrole", description="Add a role to a user")
    @app_commands.describe(member="Member to add role to", role="Role to add")
    @mod_only()
    async def addrole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't add a role that is higher than or equal to your highest role.", ephemeral=True)
            return
        
        if role in member.roles:
            await interaction.response.send_message(f"❌ {member.mention} already has {role.mention}.", ephemeral=True)
            return
        
        try:
            await member.add_roles(role, reason=f"Added by {interaction.user}")
            await interaction.response.send_message(f"✅ Added {role.mention} to {member.mention}.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to add that role.", ephemeral=True)

    @app_commands.command(name="removerole", description="Remove a role from a user")
    @app_commands.describe(member="Member to remove role from", role="Role to remove")
    @mod_only()
    async def removerole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You can't remove a role that is higher than or equal to your highest role.", ephemeral=True)
            return
        
        if role not in member.roles:
            await interaction.response.send_message(f"❌ {member.mention} doesn't have {role.mention}.", ephemeral=True)
            return
        
        try:
            await member.remove_roles(role, reason=f"Removed by {interaction.user}")
            await interaction.response.send_message(f"✅ Removed {role.mention} from {member.mention}.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to remove that role.", ephemeral=True)

    @app_commands.command(name="emojisteal", description="Add an emoji from another server")
    @app_commands.describe(emoji="The emoji to steal (from any server bot is in)", name="Custom name for the emoji")
    @admin_only()
    async def emojisteal(self, interaction: discord.Interaction, emoji: str, name: Optional[str] = None):
        # Extract emoji ID and animated status
        emoji_pattern = re.compile(r'<(a?):(\w+):(\d+)>')
        match = emoji_pattern.match(emoji)
        
        if not match:
            await interaction.response.send_message("❌ Please use a custom emoji from another server (e.g., <:name:123456789>).", ephemeral=True)
            return
        
        animated = match.group(1) == 'a'
        emoji_id = match.group(3)
        emoji_name = name or match.group(2)
        
        # Construct URL
        ext = 'gif' if animated else 'png'
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
        
        # Download and add to server
        response = requests.get(url)
        if response.status_code != 200:
            await interaction.response.send_message("❌ Failed to download emoji.", ephemeral=True)
            return
        
        try:
            new_emoji = await interaction.guild.create_custom_emoji(name=emoji_name, image=response.content)
            await interaction.response.send_message(f"✅ Added emoji: {new_emoji} `:{emoji_name}:`")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create emojis. I need 'Manage Expressions' permission.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Failed to add emoji: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffUtils(bot))