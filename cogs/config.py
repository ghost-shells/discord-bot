"""
config.py — Central configuration for roles, permissions, and settings.
Now supports dynamic overrides from the Web Dashboard (MongoDB)!
"""

import os

import discord
from discord import app_commands

# ---------------------------------------------------------------------------
# Default Fallback Role names & Channels (Used if not overridden on the website)
# ---------------------------------------------------------------------------

DEFAULT_STAFF_ROLE      = "Staff"
DEFAULT_MOD_ROLE        = "Moderator"
DEFAULT_ADMIN_ROLE      = "Admin"          
DEFAULT_TRUSTED_STAFF_ROLE = "Trusted Staff" 

# Aliases so the other cogs don't break their imports!
STAFF_ROLE      = DEFAULT_STAFF_ROLE
MOD_ROLE        = DEFAULT_MOD_ROLE
ADMIN_ROLE      = DEFAULT_ADMIN_ROLE
TRUSTED_STAFF_ROLE = DEFAULT_TRUSTED_STAFF_ROLE

# Ticket seller roles & Builder roles (Needed for Cogs)
BASE_BUYING_ROLE  = "Base Seller"
BEDROCK_ROLE      = "Bedrock Seller"
SPAWNER_ROLE      = "Spawner Trader"
BUILDING_ROLE     = "Builder"
OWNER_ROLE        = "👑 Owner"

SELLER_ROLES = [BASE_BUYING_ROLE, BEDROCK_ROLE, SPAWNER_ROLE, BUILDING_ROLE]

# Channel names (Needed for Cogs)
LOG_CHANNEL = "mod-logs"

# Ticket channel prefixes
TICKET_PREFIXES = ("ticket-", "claimed-", "claim-")

# Giveaway settings fallback
GIVEAWAYS_FILE = "giveaways.json"


# ---------------------------------------------------------------------------
# Dashboard Permission Loader
# ---------------------------------------------------------------------------

def has_dashboard_override(interaction: discord.Interaction) -> bool | None:
    """Checks if a command has dashboard overrides.
    Returns True if user is allowed by dashboard, False if blocked, None if no override."""
    # Admins always bypass dashboard overrides
    if interaction.user.guild_permissions.administrator:
        return True
        
    # Check if bot has db attached (safety check)
    if not hasattr(interaction.client, 'db'):
        return None
        
    override = interaction.client.db["command_perms"].find_one({
        "guild_id": interaction.guild.id,
        "command_name": interaction.command.qualified_name
    })
    
    if override:
        allowed_roles = override.get("roles", [])
        if has_role(interaction, *allowed_roles):
            return True
        return False
    return None

# ---------------------------------------------------------------------------
# Dynamic Config Loader (Reads from MongoDB Dashboard)
# ---------------------------------------------------------------------------

def _parse_channel_id(value) -> int | None:
    if value is None or value == "" or value == "none":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_guild_config(db, guild_id: int) -> dict:
    """Fetches dynamic config from MongoDB. Falls back to defaults if not found."""
    config = db["bot_config"].find_one({"guild_id": guild_id}) if db is not None else {}
    if not config:
        config = {}

    builder_orders_id = _parse_channel_id(config.get("BUILDER_ORDERS_CHANNEL_ID"))
    if builder_orders_id is None:
        builder_orders_id = _parse_channel_id(os.getenv("BUILDER_ORDERS_CHANNEL_ID"))

    transcript_id = _parse_channel_id(config.get("TRANSCRIPT_CHANNEL_ID"))
    if transcript_id is None:
        transcript_id = _parse_channel_id(os.getenv("DEFAULT_TRANSCRIPT_CHANNEL_ID"))

    return {
        "STAFF_ROLE": config.get("STAFF_ROLE", DEFAULT_STAFF_ROLE),
        "MOD_ROLE": config.get("MOD_ROLE", DEFAULT_MOD_ROLE),
        "ADMIN_ROLE": config.get("ADMIN_ROLE", DEFAULT_ADMIN_ROLE),
        "TRUSTED_STAFF_ROLE": config.get("TRUSTED_STAFF_ROLE", DEFAULT_TRUSTED_STAFF_ROLE),
        "LOG_CHANNEL_ID": _parse_channel_id(config.get("LOG_CHANNEL_ID")),
        "TRANSCRIPT_CHANNEL_ID": transcript_id,
        "BUILDER_ORDERS_CHANNEL_ID": builder_orders_id,
    }


def resolve_role_names(db, guild_id: int, role_names: list[str]) -> list[str]:
    """Map default role name placeholders to dashboard-configured names."""
    cfg = get_guild_config(db, guild_id)
    resolved = []
    for name in role_names:
        if name == DEFAULT_STAFF_ROLE:
            resolved.append(cfg["STAFF_ROLE"])
        elif name == DEFAULT_TRUSTED_STAFF_ROLE:
            resolved.append(cfg["TRUSTED_STAFF_ROLE"])
        else:
            resolved.append(name)
    return resolved


def member_has_role(member: discord.Member, role_name: str) -> bool:
    return any(r.name == role_name for r in member.roles)

# ---------------------------------------------------------------------------
# Permission check helpers
# ---------------------------------------------------------------------------

def has_role(interaction: discord.Interaction, *role_names: str) -> bool:
    user_roles = {r.name for r in interaction.user.roles}
    return bool(user_roles & set(role_names))

def is_admin_user(interaction: discord.Interaction) -> bool:
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return (
        interaction.user.guild_permissions.administrator
        or has_role(interaction, cfg["ADMIN_ROLE"])
    )

def is_mod_user(interaction: discord.Interaction) -> bool:
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return is_admin_user(interaction) or has_role(interaction, cfg["STAFF_ROLE"], cfg["MOD_ROLE"])

def is_staff_user(interaction: discord.Interaction) -> bool:
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return is_admin_user(interaction) or has_role(interaction, cfg["STAFF_ROLE"])

# ---------------------------------------------------------------------------
# Reusable app_commands check decorators (NOW DASHBOARD AWARE!)
# ---------------------------------------------------------------------------

def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        # 1. Check if dashboard overrides this command
        override_result = has_dashboard_override(interaction)
        if override_result is True:
            return True
        if override_result is False:
            raise app_commands.CheckFailure("❌ You don't have the required role (configured on dashboard).")
        
        # 2. Fallback to default behavior
        if is_admin_user(interaction): return True
        raise app_commands.CheckFailure("❌ Admins only!")
    return app_commands.check(predicate)

def mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        override_result = has_dashboard_override(interaction)
        if override_result is True:
            return True
        if override_result is False:
            raise app_commands.CheckFailure("❌ You don't have the required role (configured on dashboard).")
            
        if is_mod_user(interaction): return True
        raise app_commands.CheckFailure("❌ You need the Moderator or Staff role.")
    return app_commands.check(predicate)

def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        override_result = has_dashboard_override(interaction)
        if override_result is True:
            return True
        if override_result is False:
            raise app_commands.CheckFailure("❌ You don't have the required role (configured on dashboard).")
            
        if is_staff_user(interaction): return True
        raise app_commands.CheckFailure("❌ Staff only!")
    return app_commands.check(predicate)