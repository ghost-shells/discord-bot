import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from cogs.config import admin_only

class Logging(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    def get_log_channel(self, guild_id: int):
        settings = self.db["log_settings"].find_one({"guild_id": guild_id})
        if settings:
            return self.bot.get_channel(settings.get("channel_id"))
        return None

    @app_commands.command(name="setlogchannel", description="Set the channel for event logs")
    @admin_only()
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.db["log_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"channel_id": channel.id}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Event logs will now be sent to {channel.mention}.")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
            
        log_channel = self.get_log_channel(message.guild.id)
        if not log_channel:
            return

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Author", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        
        content = message.content if message.content else "*No text content*"
        if len(content) > 1024:
            content = content[:1021] + "..."
        embed.add_field(name="Content", value=content, inline=False)
        
        embed.set_footer(text=f"Message ID: {message.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild:
            return
        if before.content == after.content: # Ignore embed/picture updates
            return

        log_channel = self.get_log_channel(before.guild.id)
        if not log_channel:
            return

        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Author", value=f"{before.author.mention} (`{before.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        
        before_content = before.content if before.content else "*None*"
        after_content = after.content if after.content else "*None*"
        
        if len(before_content) > 1024: before_content = before_content[:1021] + "..."
        if len(after_content) > 1024: after_content = after_content[:1021] + "..."

        embed.add_field(name="Before", value=before_content, inline=False)
        embed.add_field(name="After", value=after_content, inline=False)
        
        embed.set_footer(text=f"Message ID: {after.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        log_channel = self.get_log_channel(member.guild.id)
        if not log_channel:
            return

        embed = discord.Embed(
            title="🟢 Member Joined",
            description=f"{member.mention} (`{member.id}`)",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member count: {member.guild.member_count}")

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        log_channel = self.get_log_channel(member.guild.id)
        if not log_channel:
            return

        embed = discord.Embed(
            title="🔴 Member Left",
            description=f"{member.mention} (`{member.id}`)",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))