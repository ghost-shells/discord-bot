"""
cogs/embed_activity.py

Adds:
  /embed     - build & send a custom embed to any channel (Manage Messages required)
  /activity  - change the bot's global status/activity, e.g. "Watching the server" (owner only)

Drop this file in your cogs/ folder and add 'cogs.embed_activity' to the COGS list in bot.py.
Presence is saved to Mongo (bot_settings/"presence") so it survives restarts.
"""

import discord
from discord import app_commands
from discord.ext import commands

DEFAULT_COLOR = 0x5865F2


def parse_color(raw: str) -> int:
    if not raw:
        return DEFAULT_COLOR
    raw = raw.strip().lstrip("#")
    try:
        return int(raw, 16)
    except ValueError:
        return DEFAULT_COLOR


class EmbedModal(discord.ui.Modal, title="Send an Embed"):
    embed_title = discord.ui.TextInput(label="Title", required=False, max_length=256)
    description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph, required=False, max_length=4000
    )
    color = discord.ui.TextInput(label="Color (hex, e.g. 5865F2)", required=False, max_length=7)
    image_url = discord.ui.TextInput(label="Image URL", required=False, max_length=500)
    footer = discord.ui.TextInput(label="Footer text", required=False, max_length=200)

    def __init__(self, channel: discord.abc.Messageable):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        if not self.embed_title.value and not self.description.value:
            await interaction.response.send_message(
                "❌ Provide at least a title or description.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=self.embed_title.value or None,
            description=self.description.value or None,
            color=parse_color(self.color.value),
        )
        if self.image_url.value:
            embed.set_image(url=self.image_url.value)
        if self.footer.value:
            embed.set_footer(text=self.footer.value)

        try:
            await self.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to send messages in {self.channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Failed to send embed: {e}", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ Embed sent to {self.channel.mention}.", ephemeral=True)


class EmbedActivity(commands.Cog):
    ACTIVITY_TYPES = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
        "streaming": discord.ActivityType.streaming,
    }
    STATUS_TYPES = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /embed ──────────────────────────────────────────────────────────
    @app_commands.command(name="embed", description="Build and send a custom embed to a channel.")
    @app_commands.describe(channel="Channel to send the embed to (defaults to this channel)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        await interaction.response.send_modal(EmbedModal(target))

    @embed.error
    async def embed_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need the **Manage Messages** permission to use this.", ephemeral=True
            )

    # ── /activity ───────────────────────────────────────────────────────
    @app_commands.command(name="activity", description="Set the bot's status/activity (owner only).")
    @app_commands.describe(
        type="Activity type",
        text="Activity text, e.g. 'the server' -> Watching the server",
        status="Online status shown next to the bot",
        stream_url="Twitch/YouTube URL — only used when type is 'streaming'",
    )
    @app_commands.choices(
        type=[app_commands.Choice(name=k.title(), value=k) for k in ACTIVITY_TYPES]
    )
    @app_commands.choices(
        status=[app_commands.Choice(name=k.title(), value=k) for k in STATUS_TYPES]
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        text: str,
        status: app_commands.Choice[str] = None,
        stream_url: str = None,
    ):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Only the bot owner can change the global activity.", ephemeral=True
            )
            return

        activity_type = self.ACTIVITY_TYPES[type.value]
        discord_status = self.STATUS_TYPES[status.value] if status else discord.Status.online

        if activity_type == discord.ActivityType.streaming:
            activity = discord.Streaming(name=text, url=stream_url or "https://twitch.tv/discord")
        else:
            activity = discord.Activity(type=activity_type, name=text)

        await self.bot.change_presence(activity=activity, status=discord_status)

        if getattr(self.bot, "db", None) is not None:
            self.bot.db["bot_settings"].update_one(
                {"_id": "presence"},
                {
                    "$set": {
                        "type": type.value,
                        "text": text,
                        "status": status.value if status else "online",
                        "stream_url": stream_url,
                    }
                },
                upsert=True,
            )

        await interaction.response.send_message(
            f"✅ Activity set to **{type.name} {text}** ({discord_status}).", ephemeral=True
        )

    # ── Restore saved presence whenever the bot (re)connects ──────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self.bot, "db", None) is None:
            return
        saved = self.bot.db["bot_settings"].find_one({"_id": "presence"})
        if not saved:
            return

        activity_type = self.ACTIVITY_TYPES.get(saved.get("type"), discord.ActivityType.playing)
        discord_status = self.STATUS_TYPES.get(saved.get("status"), discord.Status.online)

        if activity_type == discord.ActivityType.streaming:
            activity = discord.Streaming(
                name=saved.get("text", ""), url=saved.get("stream_url") or "https://twitch.tv/discord"
            )
        else:
            activity = discord.Activity(type=activity_type, name=saved.get("text", ""))

        try:
            await self.bot.change_presence(activity=activity, status=discord_status)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedActivity(bot))
