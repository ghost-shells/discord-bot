import re
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import logging

from db import get_db

logger = logging.getLogger(__name__)

COLLECTION = "vouches"

# Matches a message that *starts* with the word "vouch" (case-insensitive), e.g.
# "vouch @user thanks for the trade". This is what replaces the old /vouch slash
# command — vouching now happens by just typing it in chat.
VOUCH_TRIGGER_RE = re.compile(r"^vouch\b", re.IGNORECASE)
RAW_MENTION_RE = re.compile(r"<@!?\d+>")

# Matches a "[123]" vouch-count suffix we previously appended to a nickname, so we
# can strip it back off before appending a fresh one instead of stacking suffixes.
NICK_SUFFIX_RE = re.compile(r"\s*\[\d+\]\s*$")


def _ensure_indexes(db):
    if db is None:
        return
    try:
        db[COLLECTION].create_index([("guild_id", 1), ("vouched_id", 1)])
        db[COLLECTION].create_index([("guild_id", 1), ("voucher_id", 1)])
        db[COLLECTION].create_index([("source_message_id", 1)])
    except Exception as e:
        logger.error(f"Failed to create vouches indexes: {e}")


class Vouches(commands.Cog):
    """
    Vouching happens by typing a plain message: `vouch @member reason`.
    /vouches <user>       — view a user's vouches.
    /addvouches <user>    — [Admin] manually add vouches.
    /removevouches <user> — [Admin] manually remove vouches.
    /scan vouches         — [Admin] backfill vouches from channel history.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_indexes(self.bot.db)

    def _db(self):
        # Reuse the bot's shared db handle, refreshing if it dropped.
        db = getattr(self.bot, "db", None)
        if db is None:
            db = get_db()
            self.bot.db = db
        return db

    # ── Nickname syncing ────────────────────────────────────────────────
    async def _sync_nickname(self, member: discord.Member, count: int):
        """
        Keep the member's nickname as '<their existing name> [<count>]', without
        stacking multiple suffixes on repeated updates. If count is 0, the
        suffix is removed entirely instead of showing "[0]".
        """
        try:
            current = member.nick or member.name
            base = NICK_SUFFIX_RE.sub("", current).strip()
            if not base:
                base = member.name

            if count > 0:
                suffix = f" [{count}]"
                max_base_len = 32 - len(suffix)
                trimmed_base = base[:max_base_len].rstrip() if len(base) > max_base_len else base
                new_nick = f"{trimmed_base}{suffix}"
            else:
                new_nick = base

            # Nothing to change (and avoid an unnecessary API call/rate limit hit)
            current_effective = member.nick or member.name
            if current_effective == new_nick:
                return

            await member.edit(nick=new_nick, reason="Vouch count updated")
        except discord.Forbidden:
            logger.warning(
                f"Vouches: missing permission to rename {member.id} in guild {member.guild.id} "
                f"(check role hierarchy / Manage Nicknames)."
            )
        except discord.HTTPException as e:
            logger.error(f"Vouches: failed to update nickname for {member.id}: {e}")

    def _extract_reason(self, content: str) -> str:
        """Strip the leading 'vouch' keyword and any raw mention tokens, leaving the reason text."""
        reason = VOUCH_TRIGGER_RE.sub("", content, count=1).strip()
        reason = RAW_MENTION_RE.sub("", reason).strip()
        return reason

    # ── Plain-message vouch trigger (replaces /vouch) ──────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not VOUCH_TRIGGER_RE.match(message.content.strip()):
            return
        await self._handle_vouch_trigger(message)

    async def _handle_vouch_trigger(self, message: discord.Message):
        db = self._db()
        if db is None:
            return  # fail quietly for a casual chat trigger

        mentioned = [m for m in message.mentions if not m.bot and m.id != message.author.id]
        if not mentioned:
            if message.mentions:
                # They mentioned *something*, but it got filtered out (self or a bot)
                await message.reply(
                    "❌ You can't vouch for yourself or a bot.", mention_author=False, delete_after=8
                )
            return

        user = mentioned[0]
        reason = self._extract_reason(message.content) or "No reason given"
        reason = reason[:300]

        doc = {
            "guild_id": message.guild.id,
            "voucher_id": message.author.id,
            "vouched_id": user.id,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
            "source_message_id": message.id,
        }
        try:
            db[COLLECTION].insert_one(doc)
        except Exception as e:
            logger.error(f"Failed to insert vouch (chat trigger): {e}")
            return

        try:
            count = db[COLLECTION].count_documents({"guild_id": message.guild.id, "vouched_id": user.id})
        except Exception:
            count = None

        if count is not None:
            await self._sync_nickname(user, count)

        embed = discord.Embed(
            title="✅ Vouch added",
            description=f"{message.author.mention} vouched for {user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        if count is not None:
            embed.set_footer(text=f"{user.display_name} now has {count} vouch{'es' if count != 1 else ''}")

        try:
            await message.reply(embed=embed, mention_author=False)
        except discord.HTTPException as e:
            logger.error(f"Vouches: failed to send confirmation for {message.id}: {e}")

    # ── /vouches (view) ─────────────────────────────────────────────────
    async def _show_vouches(self, interaction: discord.Interaction, user: discord.Member):
        db = self._db()
        if db is None:
            await interaction.response.send_message(
                "❌ Database isn't available right now. Try again later.", ephemeral=True
            )
            return

        try:
            count = db[COLLECTION].count_documents(
                {"guild_id": interaction.guild_id, "vouched_id": user.id}
            )
            recent = list(
                db[COLLECTION]
                .find({"guild_id": interaction.guild_id, "vouched_id": user.id})
                .sort("timestamp", -1)
                .limit(10)
            )
        except Exception as e:
            logger.error(f"Failed to fetch vouches for {user.id}: {e}")
            await interaction.response.send_message("❌ Failed to fetch vouches. Try again later.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 {user.display_name}'s Vouches",
            description=f"**Total vouches:** `{count}`",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        if recent:
            lines = []
            for v in recent:
                voucher_id = v.get("voucher_id")
                reason = v.get("reason", "*no reason given*")
                ts = v.get("timestamp")
                ts_str = f"<t:{int(ts.timestamp())}:R>" if isinstance(ts, datetime) else ""
                lines.append(f"• <@{voucher_id}> — {reason} {ts_str}")
            embed.add_field(name="Recent vouches", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent vouches", value="No vouches yet.", inline=False)

        await interaction.response.send_message(embed=embed)

    # ── /addvouches, /removevouches (admin) ─────────────────────────────
    async def _admin_add(
        self, interaction: discord.Interaction, user: discord.Member, amount: int, reason: str | None
    ):
        db = self._db()
        if db is None:
            await interaction.response.send_message(
                "❌ Database isn't available right now. Try again later.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)
            return
        if amount > 100:
            await interaction.response.send_message("❌ Amount can't exceed 100 at once.", ephemeral=True)
            return

        clean_reason = (reason or "Manually added by staff").strip()[:300]
        now = datetime.now(timezone.utc)
        docs = [
            {
                "guild_id": interaction.guild_id,
                "voucher_id": interaction.user.id,
                "vouched_id": user.id,
                "reason": clean_reason,
                "timestamp": now,
                "manual": True,
            }
            for _ in range(amount)
        ]

        try:
            db[COLLECTION].insert_many(docs)
        except Exception as e:
            logger.error(f"Failed to insert manual vouches: {e}")
            await interaction.response.send_message("❌ Failed to add vouches. Try again later.", ephemeral=True)
            return

        try:
            count = db[COLLECTION].count_documents(
                {"guild_id": interaction.guild_id, "vouched_id": user.id}
            )
        except Exception:
            count = None

        if count is not None:
            await self._sync_nickname(user, count)

        embed = discord.Embed(
            title="✅ Vouches added",
            description=f"{interaction.user.mention} added `{amount}` vouch{'es' if amount != 1 else ''} to {user.mention}",
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.add_field(name="Reason", value=clean_reason, inline=False)
        if count is not None:
            embed.set_footer(text=f"{user.display_name} now has {count} vouch{'es' if count != 1 else ''}")

        await interaction.response.send_message(embed=embed)

    async def _admin_remove(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        db = self._db()
        if db is None:
            await interaction.response.send_message(
                "❌ Database isn't available right now. Try again later.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)
            return

        try:
            to_remove = list(
                db[COLLECTION]
                .find({"guild_id": interaction.guild_id, "vouched_id": user.id})
                .sort("timestamp", -1)
                .limit(amount)
            )
            ids = [d["_id"] for d in to_remove]
            removed = 0
            if ids:
                result = db[COLLECTION].delete_many({"_id": {"$in": ids}})
                removed = result.deleted_count
        except Exception as e:
            logger.error(f"Failed to remove vouches: {e}")
            await interaction.response.send_message("❌ Failed to remove vouches. Try again later.", ephemeral=True)
            return

        try:
            count = db[COLLECTION].count_documents(
                {"guild_id": interaction.guild_id, "vouched_id": user.id}
            )
        except Exception:
            count = None

        if count is not None:
            await self._sync_nickname(user, count)

        embed = discord.Embed(
            title="✅ Vouches removed" if removed else "ℹ️ Nothing to remove",
            description=(
                f"{interaction.user.mention} removed `{removed}` vouch{'es' if removed != 1 else ''} "
                f"from {user.mention}"
                if removed
                else f"{user.mention} has no vouches to remove."
            ),
            color=discord.Color.orange() if removed else discord.Color.light_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        if count is not None:
            embed.set_footer(text=f"{user.display_name} now has {count} vouch{'es' if count != 1 else ''}")

        await interaction.response.send_message(embed=embed)

    # ── /scan vouches (admin backfill) ──────────────────────────────────
    async def _scan_vouches(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        limit: int,
    ):
        db = self._db()
        if db is None:
            return await interaction.followup.send("❌ Database isn't available right now. Try again later.")

        scanned = 0
        added = 0
        skipped_existing = 0
        affected_user_ids: set[int] = set()

        try:
            async for msg in channel.history(limit=limit):
                scanned += 1
                if msg.author.bot:
                    continue
                if not VOUCH_TRIGGER_RE.match(msg.content.strip()):
                    continue

                mentioned = [m for m in msg.mentions if not m.bot and m.id != msg.author.id]
                if not mentioned:
                    continue
                user = mentioned[0]

                # Skip if this exact message was already recorded (live trigger or an earlier scan)
                try:
                    if db[COLLECTION].find_one({"source_message_id": msg.id}):
                        skipped_existing += 1
                        continue
                except Exception as e:
                    logger.error(f"scan_vouches: lookup failed for {msg.id}: {e}")
                    continue

                reason = self._extract_reason(msg.content) or "No reason given"
                reason = reason[:300]

                doc = {
                    "guild_id": msg.guild.id,
                    "voucher_id": msg.author.id,
                    "vouched_id": user.id,
                    "reason": reason,
                    "timestamp": msg.created_at,
                    "source_message_id": msg.id,
                    "scanned": True,
                }
                try:
                    db[COLLECTION].insert_one(doc)
                    added += 1
                    affected_user_ids.add(user.id)
                except Exception as e:
                    logger.error(f"scan_vouches: insert failed for {msg.id}: {e}")
        except discord.HTTPException as e:
            logger.error(f"scan_vouches: failed to read history in {channel.id}: {e}")
            return await interaction.followup.send(f"❌ Failed to read message history: {e}")

        for uid in affected_user_ids:
            member = channel.guild.get_member(uid)
            if not member:
                continue
            try:
                count = db[COLLECTION].count_documents({"guild_id": channel.guild.id, "vouched_id": uid})
            except Exception:
                continue
            await self._sync_nickname(member, count)

        embed = discord.Embed(
            title="🔍 Vouch Scan Complete",
            description=f"Scanned `{scanned}` message{'s' if scanned != 1 else ''} in {channel.mention}.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Vouches added", value=str(added), inline=True)
        embed.add_field(name="Already recorded (skipped)", value=str(skipped_existing), inline=True)
        embed.add_field(name="Members affected", value=str(len(affected_user_ids)), inline=True)
        await interaction.followup.send(embed=embed)

    # ── Slash Commands ───────────────────────────────────────────────────
    @app_commands.command(name="vouches", description="View a user's vouches.")
    @app_commands.describe(user="The user to look up")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def vouches(self, interaction: discord.Interaction, user: discord.Member):
        await self._show_vouches(interaction, user)

    @app_commands.command(name="addvouches", description="[Admin] Manually add vouches to a user.")
    @app_commands.describe(
        user="The user to add vouches to",
        amount="How many vouches to add (default 1)",
        reason="Reason for the manual vouch(es)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def addvouches(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 100] = 1,
        reason: str | None = None,
    ):
        await self._admin_add(interaction, user, amount, reason)

    @app_commands.command(name="removevouches", description="[Admin] Manually remove vouches from a user.")
    @app_commands.describe(
        user="The user to remove vouches from",
        amount="How many of their most recent vouches to remove (default 1)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def removevouches(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 100] = 1,
    ):
        await self._admin_remove(interaction, user, amount)

    scan_group = app_commands.Group(name="scan", description="Scan tools", guild_only=True)

    @scan_group.command(name="vouches", description="[Admin] Scan channel history for vouch messages and add any that aren't recorded yet.")
    @app_commands.describe(
        channel="Channel to scan (defaults to the current channel)",
        limit="How many recent messages to scan (default 2000, max 10000)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def scan_vouches(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        limit: app_commands.Range[int, 1, 10000] = 2000,
    ):
        await interaction.response.defer(thinking=True)
        target = channel or interaction.channel
        await self._scan_vouches(interaction, target, limit)

    @addvouches.error
    @removevouches.error
    @scan_vouches.error
    async def admin_vouch_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need Administrator permission to use this command.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Vouches(bot))
