# cogs/mcpay.py
# /linkmc  — link your MC IGN to your Discord (verifies it exists on DonutSMP)
# /unlinkmc — remove the link
# /pay     — Trusted Staff only; verifies linked account + target IGN, then runs /pay in-game
# /run     — Trusted Staff only; runs any raw command in-game and shows the captured output

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import os
import logging

from cogs.building import get_player_balance, parse_price
from cogs.config import get_guild_config

logger = logging.getLogger(__name__)

MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_trusted_staff(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    role_name = cfg.get("TRUSTED_STAFF_ROLE")
    if not role_name:
        return False
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    return role in interaction.user.roles if role else False


def get_linked_ign(db, discord_id: int) -> str | None:
    doc = db["mc_links"].find_one({"discord_id": discord_id})
    return doc.get("ign") if doc else None


async def verify_ign_exists(ign: str) -> bool:
    return await get_player_balance(ign) is not None


async def send_mc_command(command: str, capture_ms: int = 2000) -> tuple[bool, str, list[str]]:
    """Runs a command in-game and returns (success, error, output_lines).

    output_lines is whatever chat/system messages the bot saw in-game during
    the capture window — this can include other players talking, not just
    the server's response to this specific command.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MC_BOT_URL}/run-command",
                json={"command": command, "captureMs": capture_ms},
                timeout=aiohttp.ClientTimeout(total=(capture_ms / 1000) + 10),
            ) as resp:
                data = await resp.json()
                return resp.status == 200, data.get("error", ""), data.get("output", [])
    except Exception as e:
        logger.error(f"MC command failed '{command}': {e}")
        return False, str(e), []


def format_output(lines: list[str], limit: int = 1000) -> str:
    """Joins captured output lines into a Discord-safe code block."""
    if not lines:
        return "*(no output captured)*"
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"```{text}```"


# ── Cog ───────────────────────────────────────────────────────────────────────

class McPay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /linkmc
    @app_commands.command(name="linkmc", description="Link your Minecraft IGN to your Discord account")
    @app_commands.describe(ign="Your in-game name on DonutSMP")
    async def linkmc(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer(ephemeral=True)

        if not await verify_ign_exists(ign):
            return await interaction.followup.send(
                f"❌ `{ign}` wasn't found on DonutSMP. Check the spelling and try again.",
                ephemeral=True,
            )

        interaction.client.db["mc_links"].update_one(
            {"discord_id": interaction.user.id},
            {"$set": {"discord_id": interaction.user.id, "ign": ign}},
            upsert=True,
        )
        await interaction.followup.send(
            f"✅ Linked your Discord to **{ign}** on DonutSMP!", ephemeral=True
        )

    # /unlinkmc
    @app_commands.command(name="unlinkmc", description="Unlink your Minecraft account")
    async def unlinkmc(self, interaction: discord.Interaction):
        result = interaction.client.db["mc_links"].delete_one({"discord_id": interaction.user.id})
        if result.deleted_count:
            await interaction.response.send_message("✅ Your Minecraft account has been unlinked.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ You don't have a linked Minecraft account.", ephemeral=True)

    # /run
    @app_commands.command(name="run", description="Run a raw command in-game and see the output (Trusted Staff only)")
    @app_commands.describe(command="The exact command to run, e.g. /pay Notch 1000 or /kit starter")
    async def run(self, interaction: discord.Interaction, command: str):

        # 1. Permission check
        if not is_trusted_staff(interaction):
            return await interaction.response.send_message(
                "❌ You need the Trusted Staff role to use this command.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 2. Fire the raw command and capture what the server prints back
        success, err, output = await send_mc_command(command)
        if not success:
            hint = "\n\n💡 Go to the dashboard `/mc-login` page to connect the MC bot first." \
                   if "not ready" in err.lower() else ""
            return await interaction.followup.send(
                f"❌ Failed to run command: `{err}`{hint}", ephemeral=True
            )

        # 3. Show what happened in-game
        embed = discord.Embed(title="🖥️ Command Run", color=0xE67E22)
        embed.add_field(name="Command", value=f"`{command}`", inline=False)
        embed.add_field(name="In-game output", value=format_output(output), inline=False)
        embed.set_footer(text=f"Run by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # 4. Log channel
        cfg = get_guild_config(interaction.client.db, interaction.guild.id)
        log_channel_id = cfg.get("LOG_CHANNEL_ID")
        if log_channel_id:
            ch = interaction.guild.get_channel(int(log_channel_id))
            if ch:
                log = discord.Embed(
                    title="🖥️ Raw Command Run",
                    description=f"{interaction.user.mention} ran: `{command}`",
                    color=0xE67E22,
                )
                log.add_field(name="Output", value=format_output(output), inline=False)
                try:
                    await ch.send(embed=log)
                except Exception:
                    pass

    # /pay
    @app_commands.command(name="pay", description="Send in-game money to a DonutSMP player via the bot account")
    @app_commands.describe(
        ign="The Minecraft IGN to pay",
        amount="Amount to pay (e.g. 1000, 500k, 1.5m)",
    )
    async def pay(self, interaction: discord.Interaction, ign: str, amount: str):

        # 1. Permission check
        if not is_trusted_staff(interaction):
            return await interaction.response.send_message(
                "❌ You need the Trusted Staff role to use this command.", ephemeral=True
            )

        # 2. Must have a linked MC account
        linked_ign = get_linked_ign(interaction.client.db, interaction.user.id)
        if not linked_ign:
            return await interaction.response.send_message(
                "❌ You don't have a Minecraft account linked. Use `/linkmc <ign>` first.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        # 3. Verify target IGN exists on DonutSMP
        if not await verify_ign_exists(ign):
            return await interaction.followup.send(
                f"❌ `{ign}` wasn't found on DonutSMP. Double-check the IGN.", ephemeral=True
            )

        # 4. Parse amount
        parsed = parse_price(amount)
        if parsed is None:
            return await interaction.followup.send(
                "❌ Invalid amount. Use formats like `1000`, `500k`, or `1.5m`.", ephemeral=True
            )
        amount_int = int(parsed)

        # 5. Fire the in-game command
        success, err, output = await send_mc_command(f"/pay {ign} {amount_int}")
        if not success:
            hint = "\n\n💡 Go to the dashboard `/mc-login` page to connect the MC bot first." \
                   if "not ready" in err.lower() else ""
            return await interaction.followup.send(
                f"❌ Failed to send payment: `{err}`{hint}", ephemeral=True
            )

        # 6. Success
        embed = discord.Embed(title="💸 Payment Sent", color=0x2ECC71)
        embed.add_field(name="To",     value=f"`{ign}`",           inline=True)
        embed.add_field(name="Amount", value=f"`${amount_int:,}`", inline=True)
        embed.add_field(name="By",     value=interaction.user.mention, inline=True)
        embed.add_field(name="In-game output", value=format_output(output), inline=False)
        embed.set_footer(text=f"Sent from linked account: {linked_ign}")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # 7. Log channel
        cfg = get_guild_config(interaction.client.db, interaction.guild.id)
        log_channel_id = cfg.get("LOG_CHANNEL_ID")
        if log_channel_id:
            ch = interaction.guild.get_channel(int(log_channel_id))
            if ch:
                log = discord.Embed(
                    title="💸 In-Game Payment",
                    description=(
                        f"{interaction.user.mention} (`{linked_ign}`) paid "
                        f"**${amount_int:,}** to `{ign}` via the MC bot."
                    ),
                    color=0x3498DB,
                )
                try:
                    await ch.send(embed=log)
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(McPay(bot))