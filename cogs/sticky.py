import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from cogs.config import staff_only

_stickies: dict[int, dict] = {}

def load_stickies(db):
    global _stickies
    try:
        collection = db["stickies"]
        for doc in collection.find():
            _stickies[doc["channel_id"]] = {"message_id": doc["message_id"], "content": doc["content"]}
        print(f"✅ Loaded {len(_stickies)} stickies from MongoDB")
    except Exception as e:
        print(f"❌ Failed to load stickies: {e}")

def save_sticky(db, channel_id: int, message_id: int, content: str):
    try:
        db["stickies"].update_one(
            {"channel_id": channel_id},
            {"$set": {"message_id": message_id, "content": content}},
            upsert=True
        )
    except Exception as e:
        print(f"❌ Failed to save sticky: {e}")

def delete_sticky(db, channel_id: int):
    try:
        db["stickies"].delete_one({"channel_id": channel_id})
    except Exception as e:
        print(f"❌ Failed to delete sticky: {e}")

class Sticky(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        load_stickies(self.bot.db)
        # Per-channel debounce tasks + locks. When several messages land in a channel
        # in quick succession (e.g. a command that sends an embed, then a view, then
        # a role ping — all as separate messages), each one used to trigger its own
        # immediate delete-and-repost. Those overlapping reposts raced each other:
        # each read the "current" sticky message id before the previous repost had
        # finished updating it, so several of them ended up posting a fresh sticky
        # instead of cleaning up the previous one — leaving a pile of duplicates.
        # Now bursts of messages collapse into a single delayed repost.
        self._pending_tasks: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_unload(self):
        for task in self._pending_tasks.values():
            if not task.done():
                task.cancel()

    # ... (Keep cog_app_command_error the same) ...
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = str(error) if isinstance(error, app_commands.CheckFailure) else f"❌ Error: {error}"
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild: return
        channel_id = message.channel.id
        if channel_id not in _stickies: return

        sticky = _stickies[channel_id]

        # Ignore the sticky message itself (prevents delete/repost infinite loop),
        # but still react to any other message, including other bot/embed messages.
        if message.id == sticky["message_id"]:
            return

        # Debounce: if a repost is already scheduled for this channel, cancel it and
        # schedule a fresh one. A whole burst of messages arriving together will only
        # ever result in one repost, run after things settle down.
        existing = self._pending_tasks.get(channel_id)
        if existing and not existing.done():
            existing.cancel()
        self._pending_tasks[channel_id] = self.bot.loop.create_task(
            self._repost_sticky(message.channel)
        )

    async def _repost_sticky(self, channel: discord.TextChannel):
        try:
            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            return  # a newer message came in — a later task will handle the repost

        channel_id = channel.id
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            sticky = _stickies.get(channel_id)
            if not sticky:
                return  # sticky was removed while we were waiting
            content = sticky["content"]

            # Don't trust a single remembered message id — instead look at what's
            # actually in the channel. This is what actually fixes the "keeps
            # posting new stickies and never deletes the old ones" bug: previously,
            # if fetch/delete of the tracked id ever failed for any reason (stale id,
            # a permissions hiccup, another process racing us), the code silently
            # swallowed that failure and posted a new sticky anyway — every single
            # time — with nothing ever cleaning up the old ones. Now we scan the
            # recent history for every message that matches the sticky content and
            # remove all of them, so the channel can never end up with duplicates
            # no matter what caused the old id to go stale.
            duplicates = []
            last_msg_id = None
            try:
                async for msg in channel.history(limit=15):
                    if last_msg_id is None:
                        last_msg_id = msg.id
                    if msg.author.id == self.bot.user.id and msg.content == content:
                        duplicates.append(msg)
            except discord.HTTPException as e:
                print(f"❌ Sticky: failed to scan history in {channel_id}: {e}")

            # Already correct and nothing to clean up — skip the pointless repost.
            if len(duplicates) == 1 and duplicates[0].id == last_msg_id:
                if sticky["message_id"] != duplicates[0].id:
                    _stickies[channel_id]["message_id"] = duplicates[0].id
                    save_sticky(self.bot.db, channel_id, duplicates[0].id, content)
                return

            for msg in duplicates:
                try:
                    await msg.delete()
                except discord.HTTPException as e:
                    print(f"❌ Sticky: failed to delete old sticky message {msg.id} in {channel_id}: {e}")

            try:
                new_msg = await channel.send(content)
            except discord.HTTPException as e:
                print(f"❌ Sticky: failed to send repost in {channel_id}: {e}")
                return

            _stickies[channel_id]["message_id"] = new_msg.id
            save_sticky(self.bot.db, channel_id, new_msg.id, content)

    @app_commands.command(name="sticky", description="Set a sticky message in a channel")
    @app_commands.describe(channel="Channel to post the sticky in", message="The message to sticky")
    @staff_only()
    async def sticky(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        # Cancel any pending repost for this channel so it doesn't fire with stale content
        existing = self._pending_tasks.get(channel.id)
        if existing and not existing.done():
            existing.cancel()

        if channel.id in _stickies:
            try:
                old_msg = await channel.fetch_message(_stickies[channel.id]["message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException): pass

        sent = await channel.send(message)
        _stickies[channel.id] = {"message_id": sent.id, "content": message}
        save_sticky(self.bot.db, channel.id, sent.id, message)
        await interaction.response.send_message(f"✅ Sticky message set in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="unsticky", description="Remove the sticky message from a channel")
    @app_commands.describe(channel="Channel to remove the sticky from")
    @staff_only()
    async def unsticky(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if channel.id not in _stickies:
            await interaction.response.send_message(f"❌ No sticky message found in {channel.mention}.", ephemeral=True)
            return

        # Cancel any pending repost so it doesn't recreate the sticky after we remove it
        existing = self._pending_tasks.get(channel.id)
        if existing and not existing.done():
            existing.cancel()

        try:
            old_msg = await channel.fetch_message(_stickies[channel.id]["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException): pass

        del _stickies[channel.id]
        delete_sticky(self.bot.db, channel.id)
        await interaction.response.send_message(f"✅ Sticky message removed from {channel.mention}.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Sticky(bot))
