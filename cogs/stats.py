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

STAT_KEYS = {
    "balance":          ["money", "balance", "coins"],
    "shards":           ["shards"],
    "kills":            ["kills", "player_kills", "playerKills"],
    "deaths":           ["deaths"],
    "playtime":         ["playtime", "play_time", "playTime", "time_played"],
    "blocks_placed":    ["blocks_placed", "placed_blocks", "blocksPlaced"],
    "blocks_broken":    ["blocks_broken", "broken_blocks", "blocksBroken"],
    "mobs_killed":      ["mobs_killed", "mob_kills", "mobsKilled", "mobkills"],
    "shop_spent":       ["shop_spent", "money_spent", "moneySpent", "bought"],
    "shop_earned":      ["sell", "money_made", "moneyMade", "sold", "shop_earnings"],
    "vouches":          ["vouches", "vouch_count"],
    "world":            ["world", "current_world"],
}


def _extract(data: dict, keys: list):
    if not isinstance(data, dict):
        return None
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    if "result" in data and isinstance(data["result"], dict):
        for k in keys:
            if k in data["result"] and data["result"][k] is not None:
                return data["result"][k]
    return None


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
                return data
        except Exception as e:
            logger.error(f"DonutSMP stats API request failed for {ign}: {e}")
            return None


def parsed_stats(raw: dict) -> dict:
    out = {}
    for stat, keys in STAT_KEYS.items():
        out[stat] = _extract(raw, keys)
    return out


def fmt_num(n):
    if n is None:
        return "N/A"
    try:
        n = float(n)
    except (TypeError, ValueError):
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


def fmt_playtime(seconds):
    if seconds is None:
        return "N/A"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "N/A"
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
    try:
        delta = float(current) - float(previous)
    except (TypeError, ValueError):
        return "(0 / 24h)"
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

        embed.add_field(
            name="\u200b",
            value=(
                f"💰 **Balance:** `{fmt_num(stats['balance'])}` {fmt_delta(stats['balance'], previous.get('balance') if previous else None)}\n"
                f"💎 **Shards:** `{fmt_num(stats['shards'])}` {fmt_delta(stats['shards'], previous.get('shards') if previous else None)}\n"
                f"⚔️ **Kills:** `{fmt_num(stats['kills'])}` {fmt_delta(stats['kills'], previous.get('kills') if previous else None)}\n"
                f"💀 **Deaths:** `{fmt_num(stats['deaths'])}` {fmt_delta(stats['deaths'], previous.get('deaths') if previous else None)}\n"
                f"⏱️ **Playtime:** `{fmt_playtime(stats['playtime'])}`\n"
                f"🧱 **Blocks Placed:** `{fmt_num(stats['blocks_placed'])}` {fmt_delta(stats['blocks_placed'], previous.get('blocks_placed') if previous else None)}\n"
                f"⛏️ **Blocks Broken:** `{fmt_num(stats['blocks_broken'])}` {fmt_delta(stats['blocks_broken'], previous.get('blocks_broken') if previous else None)}\n"
                f"🐷 **Mobs Killed:** `{fmt_num(stats['mobs_killed'])}` {fmt_delta(stats['mobs_killed'], previous.get('mobs_killed') if previous else None)}\n"
                f"🛒 **Money Spent (Shop):** `{fmt_num(stats['shop_spent'])}` {fmt_delta(stats['shop_spent'], previous.get('shop_spent') if previous else None)}\n"
                f"💵 **Money Made (Sell):** `{fmt_num(stats['shop_earned'])}` {fmt_delta(stats['shop_earned'], previous.get('shop_earned') if previous else None)}\n"
                f"✅ **Vouches:** `{fmt_num(stats['vouches']) if stats['vouches'] is not None else 0}`"
            ),
            inline=False,
        )

        if stats.get("world"):
            embed.set_footer(text=f"{ign} is currently in the {stats['world']}")
        else:
            embed.set_footer(text=f"Stats for {ign}")

        return embed

    async def _send_stats(self, send_func, ign: str):
        raw = await fetch_stats(ign)
        if raw is None:
            await send_func(f"❌ Couldn't find stats for `{ign}`. Check the spelling or try again later.")
            return

        stats = parsed_stats(raw)
        previous = self._get_previous_snapshot(ign)
        self._save_snapshot(ign, stats)

        embed = self._build_embed(ign, stats, previous)
        await send_func(embed=embed)

    @app_commands.command(name="stats", description="View a player's DonutSMP stats.")
    @app_commands.describe(ign="Minecraft username to look up")
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