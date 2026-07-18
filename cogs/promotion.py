import discord
from discord.ext import commands
from discord import app_commands
from cogs.config import admin_only

PROMOTE_COLOR = discord.Color.from_rgb(59, 165, 93)   # green
DEMOTE_COLOR = discord.Color.from_rgb(221, 46, 68)    # red

DEFAULT_REASON = "No reason provided"


def get_config(db, guild_id: int) -> dict:
    if db is None:
        return {}
    return db["bot_config"].find_one({"guild_id": guild_id}) or {}


def build_rank_embed(
    *,
    action: str,  # "promote" or "demote"
    member: discord.Member,
    executor: discord.Member,
    new_label: str,
    reason: str,
    old_label: str = None,
) -> discord.Embed:
    is_promote = action == "promote"
    color = PROMOTE_COLOR if is_promote else DEMOTE_COLOR
    title = "⬆️ Promotion" if is_promote else "⬇️ Demotion"
    movement_arrow = "⬆️" if is_promote else "⬇️"

    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(
        name="👤 Member",
        value=f"{member.mention}\n`{member}`",
        inline=True,
    )
    embed.add_field(
        name="⭐ By",
        value=f"{executor.mention}\n`{executor.display_name}`",
        inline=True,
    )
    if old_label:
        embed.add_field(
            name="Movement",
            value=f"{movement_arrow} **{old_label}** → **{new_label}**",
            inline=False,
        )
    else:
        embed.add_field(
            name="Role Given",
            value=f"{movement_arrow} **{new_label}**",
            inline=False,
        )
    embed.add_field(
        name="❓ Reason",
        value=reason or DEFAULT_REASON,
        inline=False,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    return embed


class Promotion(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _announce(self, guild: discord.Guild, config: dict, embed: discord.Embed):
        channel_id = config.get("PROMOTE_ANNOUNCE_CHANNEL_ID")
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    @app_commands.command(name="promote", description="Promote a member by giving them a role")
    @app_commands.describe(
        member="The member to promote",
        new_role="The role to give them",
        old_role="Optional: their previous rank, shown in the announcement (not removed)",
        reason="Optional: reason for the promotion",
    )
    @admin_only()
    async def promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        new_role: discord.Role,
        old_role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if new_role in member.roles:
            await interaction.followup.send(
                f"❌ {member.mention} already has the **{new_role.name}** role.", ephemeral=True
            )
            return

        try:
            await member.add_roles(new_role, reason=f"Promoted by {interaction.user}: {reason or DEFAULT_REASON}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to manage that role. Make sure my role is above it.",
                ephemeral=True,
            )
            return

        embed = build_rank_embed(
            action="promote",
            member=member,
            executor=interaction.user,
            new_label=new_role.name,
            reason=reason,
            old_label=old_role.name if old_role else None,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
        config = get_config(self.bot.db, interaction.guild.id)
        await self._announce(interaction.guild, config, embed)

    @app_commands.command(name="demote", description="Demote a member by removing a role")
    @app_commands.describe(
        member="The member to demote",
        old_role="The role to remove from them",
        new_role="Optional: the role to drop them down to (leave blank for plain Member)",
        reason="Optional: reason for the demotion",
    )
    @admin_only()
    async def demote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        old_role: discord.Role,
        new_role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if old_role not in member.roles:
            await interaction.followup.send(
                f"❌ {member.mention} doesn't have the **{old_role.name}** role.", ephemeral=True
            )
            return

        try:
            await member.remove_roles(old_role, reason=f"Demoted by {interaction.user}: {reason or DEFAULT_REASON}")
            if new_role and new_role not in member.roles:
                await member.add_roles(new_role, reason=f"Demoted by {interaction.user}: {reason or DEFAULT_REASON}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to manage those roles. Make sure my role is above them.",
                ephemeral=True,
            )
            return

        new_label = new_role.name if new_role else "Member"
        embed = build_rank_embed(
            action="demote",
            member=member,
            executor=interaction.user,
            old_label=old_role.name,
            new_label=new_label,
            reason=reason,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
        config = get_config(self.bot.db, interaction.guild.id)
        await self._announce(interaction.guild, config, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Promotion(bot))
