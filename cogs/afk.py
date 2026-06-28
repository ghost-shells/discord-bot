import discord
from discord.ext import commands
from discord import app_commands


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # {guild_id: {user_id: reason}}
        self._afk: dict[int, dict[int, str]] = {}

    @app_commands.command(name="afk", description="Set your AFK status with an optional reason")
    @app_commands.describe(reason="Why you're going AFK")
    async def afk(self, interaction: discord.Interaction, reason: str = "No reason provided"):
        guild_id = interaction.guild.id
        if guild_id not in self._afk:
            self._afk[guild_id] = {}

        self._afk[guild_id][interaction.user.id] = reason

        embed = discord.Embed(
            description=f"💤 {interaction.user.mention} is now AFK: **{reason}**",
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        guild_afk = self._afk.get(guild_id, {})

        # If the author was AFK, remove their status
        if message.author.id in guild_afk:
            del guild_afk[message.author.id]
            try:
                await message.channel.send(
                    f"✅ Welcome back {message.author.mention}! Your AFK status has been removed.",
                    delete_after=5
                )
            except discord.HTTPException:
                pass

        # Check if any mentioned user or replied-to user is AFK
        afk_hits: list[tuple[discord.Member, str]] = []

        # Direct mentions
        for user in message.mentions:
            if user.id in guild_afk and user.id != message.author.id:
                afk_hits.append((user, guild_afk[user.id]))

        # Reply reference
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                ref_author = ref.author
                if (
                    ref_author.id in guild_afk
                    and ref_author.id != message.author.id
                    and ref_author not in message.mentions  # avoid duplicates
                ):
                    afk_hits.append((ref_author, guild_afk[ref_author.id]))

        for afk_user, reason in afk_hits:
            try:
                await message.channel.send(
                    f"💤 {afk_user.mention} is AFK: **{reason}**",
                    delete_after=5
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))