import discord
from discord import app_commands
from discord.ext import commands
from db import get_db


class Invites(commands.Cog):
    """Tracks who invited whom and exposes /invites and /invite-leaderboard."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_db()
        # In-memory cache: guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}

    # ── Helpers ──────────────────────────────────────────────────────────
    async def _cache_guild_invites(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            print(f"⚠️  Missing 'Manage Server' permission to read invites in {guild.name}")
            return
        except discord.HTTPException:
            return

        self.invite_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}

    def _add_credit(self, guild_id: int, user_id: int, amount: int):
        if self.db is None:
            return
        self.db["invite_stats"].update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$inc": {"invites": amount}},
            upsert=True,
        )

    # ── Listeners ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._cache_guild_invites(guild)
        print(f"✅ Cached invites for {len(self.invite_cache)} guild(s)")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._cache_guild_invites(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        cache = self.invite_cache.setdefault(invite.guild.id, {})
        cache[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        cache = self.invite_cache.get(invite.guild.id, {})
        cache.pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        guild = member.guild
        before = self.invite_cache.get(guild.id, {})

        try:
            after_invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return

        after = {inv.code: inv.uses or 0 for inv in after_invites}
        used_invite = None

        for inv in after_invites:
            if after.get(inv.code, 0) > before.get(inv.code, 0):
                used_invite = inv
                break

        # Update cache regardless of whether we found the match
        self.invite_cache[guild.id] = after

        if used_invite is None or used_invite.inviter is None:
            return  # vanity URL, or we couldn't determine the inviter

        inviter_id = used_invite.inviter.id

        if self.db is not None:
            self.db["invite_stats"].update_one(
                {"guild_id": guild.id, "user_id": inviter_id},
                {"$inc": {"invites": 1}, "$set": {"last_invite_code": used_invite.code}},
                upsert=True,
            )
            # Remember who invited this member so we can decrement on leave
            self.db["invite_joins"].update_one(
                {"guild_id": guild.id, "member_id": member.id},
                {"$set": {"inviter_id": inviter_id}},
                upsert=True,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if self.db is None or member.bot:
            return

        join_record = self.db["invite_joins"].find_one_and_delete(
            {"guild_id": member.guild.id, "member_id": member.id}
        )
        if join_record:
            self._add_credit(member.guild.id, join_record["inviter_id"], -1)

    # ── Slash commands ───────────────────────────────────────────────────
    @app_commands.command(name="invites", description="Check how many members someone has invited.")
    @app_commands.describe(user="Whose invite count to check (defaults to you)")
    async def invites(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user

        if self.db is None:
            await interaction.response.send_message("❌ Database unavailable.", ephemeral=True)
            return

        doc = self.db["invite_stats"].find_one({"guild_id": interaction.guild_id, "user_id": target.id})
        count = doc.get("invites", 0) if doc else 0

        embed = discord.Embed(
            title="📨 Invite Count",
            description=f"{target.mention} has **{count}** invite(s).",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="invite-leaderboard", description="See the top inviters in this server.")
    async def invite_leaderboard(self, interaction: discord.Interaction):
        if self.db is None:
            await interaction.response.send_message("❌ Database unavailable.", ephemeral=True)
            return

        await interaction.response.defer()

        docs = list(
            self.db["invite_stats"]
            .find({"guild_id": interaction.guild_id, "invites": {"$gt": 0}})
            .sort("invites", -1)
        )

        if not docs:
            await interaction.followup.send("No invite data yet for this server.")
            return

        top = docs[:10]
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, doc in enumerate(top):
            rank_icon = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{rank_icon} <@{doc['user_id']}> — **{doc['invites']}** invite(s)")

        embed = discord.Embed(
            title=f"🏆 Invite Leaderboard — {interaction.guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )

        # Show the requester's own rank if they're outside the top 10
        user_id = interaction.user.id
        if not any(d["user_id"] == user_id for d in top):
            user_rank = next((i for i, d in enumerate(docs) if d["user_id"] == user_id), None)
            if user_rank is not None:
                embed.set_footer(text=f"Your rank: #{user_rank + 1} ({docs[user_rank]['invites']} invites)")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Invites(bot))