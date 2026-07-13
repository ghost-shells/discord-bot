import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone
import aiohttp
import os
import json
import logging

from db import get_db

DONUTSMP_API_URL = os.getenv("DONUTSMP_API_URL", "https://api.donutsmp.net")
DONUTSMP_API_KEY = os.getenv("DONUTSMP_API_KEY")

logger = logging.getLogger(__name__)

# Confirmed exact field names from the DonutSMP API's /v1/stats/{ign} response,
# nested under "result". All values come back as strings.
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
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.error(f"DonutSMP stats API error for {ign}: {resp.status} - {raw}")
                    return None
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.error(f"DonutSMP stats API returned non-JSON for {ign}: {raw!r}")
                    return None
                return data.get("result")
        except Exception as e:
            logger.error(f"DonutSMP stats API request failed for {ign}: {e}")
            return None


def parsed_stats(result: dict) -> dict:
    """Turn the API's 'result' dict into our stat names, casting string numbers to floats."""
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


def fmt_delta(current, previous):
    if previous is None or current is None:
        return "(0 / 24h)"
    delta = current - previous
    sign = "+" if delta >= 0 else "-"
    return f"({sign}{fmt_num(abs(delta))} / 24h)"


class Stats(commands.Cog):
    """!stats <ign> / /stats <ign> — pulls player stats from the DonutSMP API."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_db()

    def _get_previous_snapshot(self, ign: str):
        if self.db is None:
            return None
        cutoff_low = datetime.now(timezone.utc) - timedelta(hours=27)
        cutoff_high = datetime.now(timezone.utc) - timedelta(hours=21)
        doc = self.db["stat_snapshots"].find_one(
            {
                "ign_lower": ign.lower(),
                "timestamp": {"$gte": cutoff_low, "$lte": cutoff_high},
            },
            sort=[("timestamp", 1)],
        )
        return doc.get("stats") if doc else None

    def _save_snapshot(self, ign: str, stats: dict):
        if self.db is None:
            return
        self.db["stat_snapshots"].insert_one(
            {
                "ign": ign,
                "ign_lower": ign.lower(),
                "timestamp": datetime.now(timezone.utc),
                "stats": stats,
            }
        )

    def _build_embed(self, ign: str, stats: dict, previous):
        embed = discord.Embed(
            title=f"📊 {ign}'s Statistics",
            color=discord.Color.dark_purple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/128")

        prev = previous or {}
        embed.add_field(
            name="\u200b",
            value=(
                f"💰 **Balance:** `{fmt_num(stats['balance'])}` {fmt_delta(stats['balance'], prev.get('balance'))}\n"
                f"💎 **Shards:** `{fmt_num(stats['shards'])}` {fmt_delta(stats['shards'], prev.get('shards'))}\n"
                f"⚔️ **Kills:** `{fmt_num(stats['kills'])}` {fmt_delta(stats['kills'], prev.get('kills'))}\n"
                f"💀 **Deaths:** `{fmt_num(stats['deaths'])}` {fmt_delta(stats['deaths'], prev.get('deaths'))}\n"
                f"⏱️ **Playtime:** `{fmt_playtime(stats['playtime'])}`\n"
                f"🧱 **Blocks Placed:** `{fmt_num(stats['blocks_placed'])}` {fmt_delta(stats['blocks_placed'], prev.get('blocks_placed'))}\n"
                f"⛏️ **Blocks Broken:** `{fmt_num(stats['blocks_broken'])}` {fmt_delta(stats['blocks_broken'], prev.get('blocks_broken'))}\n"
                f"🐷 **Mobs Killed:** `{fmt_num(stats['mobs_killed'])}` {fmt_delta(stats['mobs_killed'], prev.get('mobs_killed'))}\n"
                f"🛒 **Money Spent (Shop):** `{fmt_num(stats['shop_spent'])}` {fmt_delta(stats['shop_spent'], prev.get('shop_spent'))}\n"
                f"💵 **Money Made (Sell):** `{fmt_num(stats['shop_earned'])}` {fmt_delta(stats['shop_earned'], prev.get('shop_earned'))}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Stats for {ign}")
        return embed

    async def _send_stats(self, send_func, ign: str):
        result = await fetch_stats(ign)
        if result is None:
            await send_func(f"❌ Couldn't find stats for `{ign}`. Check the spelling or try again later.")
            return

        stats = parsed_stats(result)
        previous = self._get_previous_snapshot(ign)
        self._save_snapshot(ign, stats)

        embed = self._build_embed(ign, stats, previous)
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
