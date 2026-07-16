import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import logging

from db import get_db

logger = logging.getLogger(__name__)

COLLECTION = "vouches"


def _ensure_indexes(db):
    if db is None:
        return
    try:
        db[COLLECTION].create_index([("guild_id", 1), ("vouched_id", 1)])
        db[COLLECTION].create_index([("guild_id", 1), ("voucher_id", 1)])
    except Exception as e:
        logger.error(f"Failed to create vouches indexes: {e}")


class Vouches(commands.Cog):
    """/vouch <user> <reason> — vouch for a user. /vouches <user> — view a user's vouches."""

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

    async def _add_vouch(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        db = self._db()
        if db is None:
            await interaction.response.send_message(
                "❌ Database isn't available right now. Try again later.", ephemeral=True
            )
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message("❌ You can't vouch for yourself.", ephemeral=True)
            return

        if user.bot:
            await interaction.response.send_message("❌ You can't vouch for a bot.", ephemeral=True)
            return

        reason = reason.strip()
        if not reason:
            await interaction.response.send_message("❌ Please provide a reason.", ephemeral=True)
            return
        if len(reason) > 300:
            reason = reason[:300]

        doc = {
            "guild_id": interaction.guild_id,
            "voucher_id": interaction.user.id,
            "vouched_id": user.id,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
        }

        try:
            db[COLLECTION].insert_one(doc)
        except Exception as e:
            logger.error(f"Failed to insert vouch: {e}")
            await interaction.response.send_message("❌ Failed to save that vouch. Try again later.", ephemeral=True)
            return

        try:
            count = db[COLLECTION].count_documents(
                {"guild_id": interaction.guild_id, "vouched_id": user.id}
            )
        except Exception:
            count = None

        embed = discord.Embed(
            title="✅ Vouch added",
            description=f"{interaction.user.mention} vouched for {user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        if count is not None:
            embed.set_footer(text=f"{user.display_name} now has {count} vouch{'es' if count != 1 else ''}")

        await interaction.response.send_message(embed=embed)

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

    @app_commands.command(name="vouch", description="Vouch for a user.")
    @app_commands.describe(user="The user you're vouching for", reason="Why you're vouching for them")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def vouch(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        await self._add_vouch(interaction, user, reason)

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

    @addvouches.error
    @removevouches.error
    async def admin_vouch_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need Administrator permission to use this command.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Vouches(bot))
