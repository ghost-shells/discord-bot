import discord
from discord.ext import commands
from discord import app_commands
import re
from datetime import datetime, timezone
from cogs.config import admin_only, get_guild_config, member_has_role_id

class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    automod_group = app_commands.Group(name="automod", description="Configure Auto-Moderation settings")

    def get_settings(self, guild_id: int):
        return self.db["automod_settings"].find_one({"guild_id": guild_id})

    @automod_group.command(name="links", description="Toggle deleting all links")
    @app_commands.describe(enabled="True to enable, False to disable")
    @admin_only()
    async def toggle_links(self, interaction: discord.Interaction, enabled: bool):
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"block_links": enabled}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Link blocking {'enabled' if enabled else 'disabled'}.")

    @automod_group.command(name="invites", description="Toggle deleting Discord invites")
    @app_commands.describe(enabled="True to enable, False to disable")
    @admin_only()
    async def toggle_invites(self, interaction: discord.Interaction, enabled: bool):
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"block_invites": enabled}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Discord invite blocking {'enabled' if enabled else 'disabled'}.")

    @automod_group.command(name="addword", description="Add a word to the blocklist")
    @admin_only()
    async def add_word(self, interaction: discord.Interaction, word: str):
        settings = self.get_settings(interaction.guild.id)
        banned_words = settings.get("banned_words", []) if settings else []
        
        if word.lower() in banned_words:
            return await interaction.response.send_message("❌ That word is already blocked.", ephemeral=True)
            
        banned_words.append(word.lower())
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"banned_words": banned_words}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Added `{word}` to the blocklist.")

    @automod_group.command(name="removeword", description="Remove a word from the blocklist")
    @admin_only()
    async def remove_word(self, interaction: discord.Interaction, word: str):
        settings = self.get_settings(interaction.guild.id)
        banned_words = settings.get("banned_words", []) if settings else []
        
        if word.lower() not in banned_words:
            return await interaction.response.send_message("❌ That word isn't on the blocklist.", ephemeral=True)
            
        banned_words.remove(word.lower())
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"banned_words": banned_words}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Removed `{word}` from the blocklist.")

    @automod_group.command(name="listwords", description="List all blocked words")
    @admin_only()
    async def list_words(self, interaction: discord.Interaction):
        settings = self.get_settings(interaction.guild.id)
        banned_words = settings.get("banned_words", []) if settings else []
        
        if not banned_words:
            return await interaction.response.send_message("❌ The blocklist is empty.", ephemeral=True)
            
        embed = discord.Embed(
            title="🚫 Banned Words List",
            description="\n".join(f"• `{w}`" for w in banned_words),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod_group.command(name="addchannel", description="Add a channel for AutoMod to monitor")
    @admin_only()
    async def add_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        settings = self.get_settings(interaction.guild.id)
        active_channels = settings.get("active_channels", []) if settings else []
        
        if channel.id in active_channels:
            return await interaction.response.send_message("❌ That channel is already being monitored.", ephemeral=True)
            
        active_channels.append(channel.id)
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"active_channels": active_channels}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ AutoMod is now monitoring {channel.mention}.")

    @automod_group.command(name="removechannel", description="Remove a channel from AutoMod monitoring")
    @admin_only()
    async def remove_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        settings = self.get_settings(interaction.guild.id)
        active_channels = settings.get("active_channels", []) if settings else []
        
        if channel.id not in active_channels:
            return await interaction.response.send_message("❌ That channel isn't being monitored.", ephemeral=True)
            
        active_channels.remove(channel.id)
        self.db["automod_settings"].update_one(
            {"guild_id": interaction.guild.id},
            {"$set": {"active_channels": active_channels}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ AutoMod is no longer monitoring {channel.mention}.")

    @automod_group.command(name="listchannels", description="List channels AutoMod is monitoring")
    @admin_only()
    async def list_channels(self, interaction: discord.Interaction):
        settings = self.get_settings(interaction.guild.id)
        active_channels = settings.get("active_channels", []) if settings else []
        
        if not active_channels:
            return await interaction.response.send_message("❌ AutoMod isn't monitoring any channels. Add one with `/automod addchannel`.", ephemeral=True)
            
        mentions = [f"<#{cid}>" for cid in active_channels]
        embed = discord.Embed(
            title="👁️ Monitored Channels",
            description="\n".join(mentions),
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Listener & Warning Logic
    # ---------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        staff_role_id = get_guild_config(self.bot.db, message.guild.id)["STAFF_ROLE_ID"]
        if member_has_role_id(message.author, staff_role_id) or message.author.guild_permissions.administrator:
            return

        settings = self.get_settings(message.guild.id)
        if not settings:
            return
            
        active_channels = settings.get("active_channels", [])
        if message.channel.id not in active_channels:
            return # Not a monitored channel

        content_lower = message.content.lower()
        deleted_reason = None
        matched_word = None

        # 3. Check Discord Invites
        if settings.get("block_invites") and ("discord.gg/" in content_lower or "discord.com/invite/" in content_lower):
            deleted_reason = "Sending Discord invites"

        # 4. Check Links
        elif settings.get("block_links") and re.search(r'(https?://|www\.)', content_lower):
            deleted_reason = "Sending links"

        # 5. Check Banned Words
        elif not deleted_reason:
            banned_words = settings.get("banned_words", [])
            for word in banned_words:
                if word in content_lower:
                    deleted_reason = f"Using banned word (`{word}`)"
                    matched_word = word
                    break

        # 6. If a rule was broken: Delete, Warn, Save to DB
        if deleted_reason:
            try:
                await message.delete()
            except discord.HTTPException:
                pass # Message might have already been deleted

            # Create the warning entry
            entry = {
                "reason": f"AutoMod: {deleted_reason}",
                "mod": str(self.bot.user),
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }

            # Save to MongoDB (warnings collection)
            collection = self.db["warnings"]
            doc = collection.find_one({"guild_id": message.guild.id, "user_id": message.author.id})
            current_warnings = doc.get("warnings", []) if doc else []
            current_warnings.append(entry)
            
            collection.update_one(
                {"guild_id": message.guild.id, "user_id": message.author.id},
                {"$set": {"warnings": current_warnings}},
                upsert=True
            )

            # Also update the in-memory cache of the Moderation cog so /warnings shows it instantly
            mod_cog = self.bot.get_cog('Moderation')
            if mod_cog and hasattr(mod_cog, '_warnings'):
                mod_cog._warnings[message.guild.id][message.author.id].append(entry)

            # Notify the user
            try:
                await message.channel.send(f"⚠️ {message.author.mention}, you have been warned for: **{deleted_reason}**.", delete_after=5)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))