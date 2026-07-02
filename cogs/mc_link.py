"""
Per-user Minecraft account linking.

Each Discord user runs /link, signs into their OWN Microsoft account via the
device-code flow, and from then on can run /mc <command> to act as their own
linked Minecraft account. Nobody can see or drive anybody else's session —
every request to the mc-bot service is scoped to the caller's Discord ID.
"""

import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")

POLL_INTERVAL = 3     # seconds between status checks while waiting on login
POLL_TIMEOUT = 300    # give up waiting after 5 minutes

STATUS_COLORS = {
    "ready": discord.Color.green(),
    "connecting": discord.Color.blue(),
    "awaiting_auth": discord.Color.gold(),
    "awaiting_discord_auth": discord.Color.purple(),
    "disconnected": discord.Color.greyple(),
    "error": discord.Color.red(),
}


class AuthorizedView(discord.ui.View):
    """Shown while we're waiting on the DonutSMP Discord-DM authorization step."""

    def __init__(self, cog: "MCLink", discord_id: str):
        super().__init__(timeout=POLL_TIMEOUT)
        self.cog = cog
        self.discord_id = discord_id

    @discord.ui.button(label="✅ I Authorized", style=discord.ButtonStyle.blurple)
    async def authorized(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("This isn't your link session.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._post(f"/reconnect/{self.discord_id}")
        await self.cog._poll_until_settled(interaction, self.discord_id)


class MCLink(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── mc-bot HTTP helpers ─────────────────────────────────────────────────
    async def _get(self, path: str) -> dict:
        try:
            async with self.session.get(f"{MC_BOT_URL}{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
        except Exception as e:
            return {"status": "error", "error": f"MC bot unreachable: {e}"}

    async def _post(self, path: str, json: dict | None = None) -> dict:
        try:
            async with self.session.post(f"{MC_BOT_URL}{path}", json=json, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()
        except Exception as e:
            return {"ok": False, "error": f"MC bot unreachable: {e}"}

    # ── UI helpers ───────────────────────────────────────────────────────────
    def _embed(self, status: dict) -> discord.Embed:
        s = status.get("status")
        embed = discord.Embed(color=STATUS_COLORS.get(s, discord.Color.greyple()))

        if s == "ready":
            embed.title = "✅ Linked & Connected"
            embed.description = f"Playing as **{status.get('mcUsername', 'unknown')}**"
        elif s == "awaiting_auth":
            embed.title = "🔑 Microsoft Sign-In Required"
            embed.description = (
                f"Go to **{status.get('url')}** and enter this code:\n\n"
                f"## `{status.get('code')}`\n\nThis message updates automatically once you sign in."
            )
        elif s == "awaiting_discord_auth":
            embed.title = "🔔 Discord Authorization Needed"
            embed.description = (
                "DonutSMP sent you a Discord DM asking you to authorize this login.\n"
                "Check your DMs, click **Authorize**, then press the button below."
            )
        elif s == "connecting":
            embed.title = "⏳ Connecting…"
        elif s == "error":
            embed.title = "❌ Error"
            embed.description = status.get("error", "Unknown error")
        else:
            embed.title = "⚪ Not Linked"
            embed.description = "Run `/link` to connect your own Minecraft account."
        return embed

    async def _poll_until_settled(self, interaction: discord.Interaction, discord_id: str):
        elapsed = 0
        last_status = None
        while elapsed < POLL_TIMEOUT:
            status = await self._get(f"/status/{discord_id}")
            if status.get("status") != last_status:
                view = AuthorizedView(self, discord_id) if status.get("status") == "awaiting_discord_auth" else None
                try:
                    await interaction.edit_original_response(embed=self._embed(status), view=view)
                except discord.HTTPException:
                    pass
                last_status = status.get("status")
            if status.get("status") in ("ready", "error"):
                return
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        try:
            await interaction.edit_original_response(
                content="⏱️ Timed out waiting for login. Run `/link` again when you're ready.",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            pass

    # ── Commands ─────────────────────────────────────────────────────────────
    @app_commands.command(name="link", description="Link your own Minecraft (Microsoft) account to the bot.")
    async def link(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)

        current = await self._get(f"/status/{discord_id}")
        if current.get("status") == "ready":
            await interaction.edit_original_response(embed=self._embed(current))
            return

        await self._post(f"/start-login/{discord_id}")
        await interaction.edit_original_response(embed=discord.Embed(title="⏳ Starting…"))
        await self._poll_until_settled(interaction, discord_id)

    @app_commands.command(name="unlink", description="Remove your linked Minecraft account (requires a fresh login next time).")
    async def unlink(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        await self._post(f"/full-logout/{discord_id}")
        await interaction.followup.send("🗑️ Your Minecraft account has been unlinked.", ephemeral=True)

    @app_commands.command(name="mcstatus", description="Check your Minecraft link status.")
    async def mcstatus(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        status = await self._get(f"/status/{discord_id}")
        await interaction.followup.send(embed=self._embed(status), ephemeral=True)

    @app_commands.command(name="mc", description="Run an in-game command as your own linked Minecraft account.")
    @app_commands.describe(command="The command to run, without a leading slash")
    async def mc(self, interaction: discord.Interaction, command: str):
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        result = await self._post(f"/run-command/{discord_id}", json={"command": command})
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Failed to run command')}", ephemeral=True)
            return
        output = result.get("output") or []
        text = "\n".join(output)[:1900] if output else "*(no output captured)*"
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MCLink(bot))
