import discord
from discord.ext import commands
from discord import app_commands
from cogs.config import admin_only

class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(name="welcome", description="Set the welcome message channel and text")
    @app_commands.describe(
        channel="Channel to send welcome messages",
        message="Welcome message (use {user}, {server}, {member_count})"
    )
    @admin_only()
    async def set_welcome(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome {user} to {server}!"):
        try:
            self.db["welcome_settings"].update_one(
                {"guild_id": interaction.guild.id},
                {"$set": {"channel_id": channel.id, "message": message}},
                upsert=True
            )
            await interaction.response.send_message(f"✅ Welcome messages will be sent to {channel.mention}!\nPreview: {message.replace('{user}', interaction.user.mention).replace('{server}', interaction.guild.name).replace('{member_count}', str(interaction.guild.member_count))}", ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to set welcome: {e}", ephemeral=True)

    @app_commands.command(name="welcome_disable", description="Disable welcome messages")
    @admin_only()
    async def disable_welcome(self, interaction: discord.Interaction):
        self.db["welcome_settings"].delete_one({"guild_id": interaction.guild.id})
        await interaction.response.send_message("✅ Welcome messages disabled.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
            
        settings = self.db["welcome_settings"].find_one({"guild_id": member.guild.id})
        if not settings:
            return

        channel = member.guild.get_channel(settings.get("channel_id"))
        if not channel:
            return

        msg_text = settings.get("message", "Welcome {user}!")
        msg_text = msg_text.replace("{user}", member.mention).replace("{server}", member.guild.name).replace("{member_count}", str(member.guild.member_count))

        embed = discord.Embed(
            title="🎉 New Member!",
            description=msg_text,
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member ID: {member.id}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))