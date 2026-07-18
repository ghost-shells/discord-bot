"""
cogs/ai_chat.py

Casual conversational AI — separate from the dashboard's AI Admin Agent.

Triggers when:
  - Someone @mentions the bot directly (not @everyone/@here), or
  - Someone replies to a message the bot sent

Uses the same free GROQ_API_KEY as the dashboard agent (via ai_agent.py's
simple_chat helper), but with NO tool access — it can only talk, never
moderate or take actions. Keeps a short rolling per-channel memory so
back-and-forth conversation feels continuous, but nothing is persisted
to Mongo (resets on bot restart, and that's fine for casual chat).
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone

import ai_agent

MAX_HISTORY_TURNS = 6       # user+assistant pairs kept per channel
COOLDOWN_SECONDS = 4        # per-user, to avoid spam/rate-limit issues
DISCORD_CHUNK = 1900        # stay under Discord's 2000 char message limit

SYSTEM_PROMPT_TEMPLATE = (
    "You are {bot_name}, a friendly, casual Discord bot chatting in the server \"{guild_name}\". "
    "Keep replies conversational and fairly short (a few sentences, unless the person clearly "
    "wants something longer or more detailed). Use a relaxed, natural tone — not corporate or "
    "overly formal. You are NOT a moderation tool in this conversation and cannot mute, kick, "
    "ban, or change anything on the server — if someone asks you to do that, tell them to use "
    "the actual mod commands or the dashboard instead. Never claim to have taken an action you "
    "didn't actually take."
)


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history: dict[int, list[dict]] = {}       # channel_id -> [{role, content}, ...]
        self._last_reply_at: dict[int, datetime] = {}    # user_id -> last reply time

    # ── Helpers ──────────────────────────────────────────────────────────
    def _get_history(self, channel_id: int) -> list[dict]:
        return self._history.setdefault(channel_id, [])

    def _trim_history(self, channel_id: int):
        hist = self._history.get(channel_id, [])
        limit = MAX_HISTORY_TURNS * 2
        if len(hist) > limit:
            self._history[channel_id] = hist[-limit:]

    def _on_cooldown(self, user_id: int) -> bool:
        now = datetime.now(timezone.utc)
        last = self._last_reply_at.get(user_id)
        if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
            return True
        self._last_reply_at[user_id] = now
        return False

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        ref = message.reference
        if not ref:
            return False
        if ref.resolved and isinstance(ref.resolved, discord.Message):
            return ref.resolved.author.id == self.bot.user.id
        # Not cached — fetch it once to check the author
        try:
            ref_msg = await message.channel.fetch_message(ref.message_id)
            return ref_msg.author.id == self.bot.user.id
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    # ── Listener ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        is_mentioned = self.bot.user in message.mentions and not message.mention_everyone
        is_reply_to_bot = await self._is_reply_to_bot(message)

        if not is_mentioned and not is_reply_to_bot:
            return

        if not ai_agent.GROQ_API_KEY:
            return  # feature not configured — stay silent rather than error in chat

        if self._on_cooldown(message.author.id):
            return

        # Strip the mention text out of the message content
        content = message.content
        for m in message.mentions:
            content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        content = content.strip() or "(no text, just a mention)"

        channel_id = message.channel.id
        history = self._get_history(channel_id)

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            bot_name=self.bot.user.display_name, guild_name=message.guild.name
        )
        messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": f"{message.author.display_name}: {content}"}]
        )

        try:
            async with message.channel.typing():
                reply = await self.bot.loop.run_in_executor(None, ai_agent.simple_chat, messages)
        except Exception as e:
            print(f"❌ AIChat: Groq call failed: {e}")
            return

        history.append({"role": "user", "content": f"{message.author.display_name}: {content}"})
        history.append({"role": "assistant", "content": reply})
        self._trim_history(channel_id)

        for start in range(0, len(reply), DISCORD_CHUNK):
            chunk = reply[start:start + DISCORD_CHUNK]
            try:
                await message.reply(chunk, mention_author=False)
            except discord.HTTPException as e:
                print(f"❌ AIChat: failed to send reply: {e}")
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
