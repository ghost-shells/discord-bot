import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import aiohttp
import os
import json
import logging

DONUTSMP_API_URL = os.getenv("DONUTSMP_API_URL", "https://api.donutsmp.net")
DONUTSMP_API_KEY = os.getenv("DONUTSMP_API_KEY")

logger = logging.getLogger(__name__)

STAT_KEYS = {
    "balance":       "money",
    "shards":        "shards",
    "kills":         "kills",
    "deaths":        "deaths",
    "playtime":      "playtime",
    "blocks_placed": "placed_blocks",
    "blocks_broken": "broken_blocks",
    "mobs_killed":   "mobs_killed",
    "shop_spent":    "money_spent_on_shop",
    "shop_earned":   "money_made_from_sell",
}


async def fetch_stats(ign: str):
    url = f"{DONUTSMP_API_URL}/v1/stats/{ign}"
    headers = {}
    if DONUTSMP_API_KEY:
        headers["Authorization"] = f"Bearer {DONUTSMP_API_KEY}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    return None
                data = json.loads(raw)
                return data.get("result")
        except Exception as e:
            logger.error(f"DonutSMP stats API request failed for {ign}: {e}")
            return None


async def fetch_location(ign: str) -> str | None:
    """
    Fetches /v1/lookup/{user}.
    Returns the location string (e.g. 'Overworld', 'Nether', 'End')
    or None if the player is offline / not found.
    """
    url = f"{DONUTSMP_API_URL}/v1/lookup/{ign}"
    headers = {}
    if DONUTSMP_API_KEY:
        headers["Authorization"] = f"Bearer {DONUTSMP_API_KEY}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    return None
                data = json.loads(raw)
                result = data.get("result", {})
                return result.get("location")  # e.g. "Overworld", "Nether", "End", or None
        except Exception as e:
            logger.error(f"DonutSMP lookup API request failed for {ign}: {e}")
            return None


def parsed_stats(result: dict) -> dict:
    out = {}
    for stat, key in STAT_KEYS.items():
        raw_val = result.get(key) if result else None
        try:
            out[stat] = float(raw_val)
        except (TypeError, ValueError):
            out[stat] = None
    return out


def fmt_num(n):
    if n is None:
        return "N/A"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.2f}K"
    return f"{sign}{n:.0f}" if n == int(n) else f"{sign}{n:.2f}"


def fmt_playtime(milliseconds):
    if milliseconds is None:
        return "N/A"
    seconds = int(milliseconds) // 1000
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


WORLD_EMOJI = {
    "overworld": "🌍",
    "nether":    "🔥",
    "end":       "🌌",
}


def _build_embed(ign: str, stats: dict, location: str | None) -> discord.Embed:
    if location:
        # Player is online
        world_key = location.lower()
        emoji = WORLD_EMOJI.get(world_key, "🌐")
        status_text = f"🟢 Online • {emoji} {location}"
        color = discord.Color.green()
    else:
        status_text = "🔴 Offline"
        color = discord.Color.dark_purple()

    embed = discord.Embed(
        title=f"📊 {ign}'s Statistics",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/128")

    embed.add_field(
        name="\u200b",
        value=(
            f"💰 **Balance:** `{fmt_num(stats['balance'])}`\n"
            f"💎 **Shards:** `{fmt_num(stats['shards'])}`\n"
            f"⚔️ **Kills:** `{fmt_num(stats['kills'])}`\n"
            f"💀 **Deaths:** `{fmt_num(stats['deaths'])}`\n"
            f"⏱️ **Playtime:** `{fmt_playtime(stats['playtime'])}`\n"
            f"🧱 **Blocks Placed:** `{fmt_num(stats['blocks_placed'])}`\n"
            f"⛏️ **Blocks Broken:** `{fmt_num(stats['blocks_broken'])}`\n"
            f"🐷 **Mobs Killed:** `{fmt_num(stats['mobs_killed'])}`\n"
            f"🛒 **Money Spent (Shop):** `{fmt_num(stats['shop_spent'])}`\n"
            f"💵 **Money Made (Sell):** `{fmt_num(stats['shop_earned'])}`"
        ),
        inline=False,
    )

    embed.set_footer(text=status_text)
    return embed


class Stats(commands.Cog):
    """!stats <ign> / /stats <ign> — pulls player stats from the DonutSMP API."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _send_stats(self, send_func, ign: str):
        # Fetch stats first — if this fails the IGN is wrong
        result = await fetch_stats(ign)
        if result is None:
            await send_func(f"❌ Couldn't find stats for `{ign}`. Check the spelling or try again later.")
            return

        stats = parsed_stats(result)

        # Now fetch location to determine online/offline status
        location = await fetch_location(ign)

        embed = _build_embed(ign, stats, location)
        await send_func(embed=embed)

    @app_commands.command(name="stats", description="View a player's DonutSMP stats.")
    @app_commands.describe(ign="Minecraft username to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_slash(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer()
        await self._send_stats(interaction.followup.send, ign)

    @commands.command(name="stats")
    async def stats_prefix(self, ctx: commands.Context, ign: str = None):
        if not ign:
            await ctx.send("❌ Usage: `!stats <ign>`")
            return
        async with ctx.typing():
            await self._send_stats(ctx.send, ign)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
