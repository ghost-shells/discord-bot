import discord
from discord.ext import commands
from discord import app_commands
from cogs.config import admin_only

_react_roles = {}

def load_react_roles(db):
    global _react_roles
    try:
        collection = db["reactionroles"]
        for doc in collection.find():
            _react_roles[str(doc["message_id"])] = doc["emoji_role_map"]
        print(f"✅ Loaded {len(_react_roles)} reaction role messages from MongoDB")
    except Exception as e:
        print(f"❌ Failed to load reaction roles: {e}")

def save_react_role(db, message_id: int, emoji_role_map: dict):
    try:
        db["reactionroles"].update_one(
            {"message_id": message_id},
            {"$set": {"emoji_role_map": emoji_role_map}},
            upsert=True
        )
    except Exception as e:
        print(f"❌ Failed to save reaction role: {e}")

def delete_react_role_message(db, message_id: int):
    try:
        db["reactionroles"].delete_one({"message_id": message_id})
    except Exception as e:
        print(f"❌ Failed to delete reaction role: {e}")

class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        load_react_roles(self.bot.db)

    rr_group = app_commands.Group(name="reactionrole", description="Manage reaction roles")

    @rr_group.command(name="add", description="Add a reaction role to a specific message")
    @app_commands.describe(message_id="The ID of the message", emoji="The emoji to use", role="The role to give")
    @admin_only()
    async def rr_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)

        try: msg_id = int(message_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid Message ID.", ephemeral=True); return

        found_msg = None
        for channel in interaction.guild.text_channels:
            try:
                found_msg = await channel.fetch_message(msg_id)
                break
            except (discord.NotFound, discord.Forbidden): continue
        
        if not found_msg:
            await interaction.followup.send("❌ Could not find that message.", ephemeral=True); return

        try: await found_msg.add_reaction(emoji)
        except (discord.HTTPException, TypeError):
            await interaction.followup.send("❌ Invalid emoji, or I don't have access to that emoji.", ephemeral=True); return

        msg_id_str = str(msg_id)
        if msg_id_str not in _react_roles: _react_roles[msg_id_str] = {}

        _react_roles[msg_id_str][emoji] = role.id
        save_react_role(self.bot.db, msg_id, _react_roles[msg_id_str])

        await interaction.followup.send(f"✅ Reaction role added!\n**Emoji:** {emoji} → **Role:** {role.mention}", ephemeral=True)

    @rr_group.command(name="remove", description="Remove a reaction role from a message")
    @app_commands.describe(message_id="The ID of the message", emoji="The emoji to remove")
    @admin_only()
    async def rr_remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        await interaction.response.defer(ephemeral=True)

        msg_id_str = str(message_id)
        if msg_id_str not in _react_roles or emoji not in _react_roles[msg_id_str]:
            await interaction.followup.send("❌ That reaction role doesn't exist.", ephemeral=True); return

        del _react_roles[msg_id_str][emoji]
        
        if not _react_roles[msg_id_str]:
            del _react_roles[msg_id_str]
            delete_react_role_message(self.bot.db, int(message_id))
        else:
            save_react_role(self.bot.db, int(message_id), _react_roles[msg_id_str])

        try:
            msg_id = int(message_id)
            for channel in interaction.guild.text_channels:
                try:
                    found_msg = await channel.fetch_message(msg_id)
                    await found_msg.remove_reaction(emoji, self.bot.user)
                    break
                except (discord.NotFound, discord.Forbidden): continue
        except Exception: pass

        await interaction.followup.send(f"✅ Reaction role removed for {emoji}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member.bot: return
        msg_id_str = str(payload.message_id)
        if msg_id_str not in _react_roles: return

        emoji_str = str(payload.emoji)
        if emoji_str not in _react_roles[msg_id_str]: return

        role_id = _react_roles[msg_id_str][emoji_str]
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id)
        if role and payload.member:
            try: await payload.member.add_roles(role, reason="Reaction Role")
            except discord.Forbidden: print(f"❌ Missing permissions to assign role {role.name}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None: return
        msg_id_str = str(payload.message_id)
        if msg_id_str not in _react_roles: return

        emoji_str = str(payload.emoji)
        if emoji_str not in _react_roles[msg_id_str]: return

        role_id = _react_roles[msg_id_str][emoji_str]
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        
        if role and member and not member.bot:
            try: await member.remove_roles(role, reason="Reaction Role removed")
            except discord.Forbidden: print(f"❌ Missing permissions to remove role {role.name}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))