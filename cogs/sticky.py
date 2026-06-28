import discord
from discord.ext import commands
from discord import app_commands
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

    # ... (Keep cog_app_command_error the same) ...
    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = str(error) if isinstance(error, app_commands.CheckFailure) else f"❌ Error: {error}"
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild: return
        channel_id = message.channel.id
        if channel_id not in _stickies: return

        sticky = _stickies[channel_id]
        try:
            old_msg = await message.channel.fetch_message(sticky["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException): pass

        new_msg = await message.channel.send(sticky["content"])
        _stickies[channel_id]["message_id"] = new_msg.id
        save_sticky(self.bot.db, channel_id, new_msg.id, sticky["content"])

    @app_commands.command(name="sticky", description="Set a sticky message in a channel")
    @app_commands.describe(channel="Channel to post the sticky in", message="The message to sticky")
    @staff_only()
    async def sticky(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
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
        try:
            old_msg = await channel.fetch_message(_stickies[channel.id]["message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException): pass

        del _stickies[channel.id]
        delete_sticky(self.bot.db, channel.id)
        await interaction.response.send_message(f"✅ Sticky message removed from {channel.mention}.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Sticky(bot))