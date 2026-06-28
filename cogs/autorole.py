import discord
from discord.ext import commands
from discord import app_commands
from cogs.config import admin_only

class Autorole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(name="autorole", description="Set the role given to new members automatically")
    @app_commands.describe(role="The role to give to new members")
    @admin_only()
    async def set_autorole(self, interaction: discord.Interaction, role: discord.Role):
        # Check if the bot can actually assign this role
        if role.position >= interaction.guild.me.top_role.position:
            return await interaction.response.send_message("❌ I cannot assign this role because it is higher than or equal to my highest role!", ephemeral=True)

        try:
            self.db["autorole_settings"].update_one(
                {"guild_id": interaction.guild.id},
                {"$set": {"role_id": role.id}},
                upsert=True
            )
            await interaction.response.send_message(f"✅ Autorole set to {role.mention}. New members will receive this role automatically.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to set autorole: {e}", ephemeral=True)

    @app_commands.command(name="autorole_disable", description="Disable the auto-role feature")
    @admin_only()
    async def disable_autorole(self, interaction: discord.Interaction):
        result = self.db["autorole_settings"].delete_one({"guild_id": interaction.guild.id})
        if result.deleted_count > 0:
            await interaction.response.send_message("✅ Autorole has been disabled.")
        else:
            await interaction.response.send_message("❌ Autorole isn't currently enabled.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Don't give roles to bots
        if member.bot:
            return

        settings = self.db["autorole_settings"].find_one({"guild_id": member.guild.id})
        if not settings:
            return

        role_id = settings.get("role_id")
        if not role_id:
            return

        role = member.guild.get_role(role_id)
        if not role:
            return # Role was deleted

        try:
            await member.add_roles(role, reason="Autorole on join")
        except discord.Forbidden:
            print(f"❌ Missing permissions to assign autorole in {member.guild.name}")
        except discord.HTTPException as e:
            print(f"❌ Failed to assign autorole: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Autorole(bot))