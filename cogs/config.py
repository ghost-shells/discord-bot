"""
config.py — Central configuration for roles, permissions, and settings.
Now supports dynamic overrides from the Web Dashboard (MongoDB)!

The Staff / Mod / Admin / Trusted Staff roles are configured on the
dashboard as real Discord roles picked from a dropdown, and stored as
role IDs (not typed-out role names). get_guild_config() exposes these as
STAFF_ROLE_ID / MOD_ROLE_ID / ADMIN_ROLE_ID / TRUSTED_STAFF_ROLE_ID.
"""

import os

import discord
from discord import app_commands

# ---------------------------------------------------------------------------
# Ticket seller roles & Builder roles (Needed for Cogs)
# These are unrelated to the dashboard-configured Staff/Mod/Admin/Trusted
# Staff roles above, and are still matched by name.
# ---------------------------------------------------------------------------

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
# Dashboard Permission Loader (per-command role overrides, unrelated to the
# Staff/Mod/Admin/Trusted Staff roles — these are still matched by name)
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

def _parse_id(value) -> int | None:
    """Parses a channel or role ID that may arrive as an int, numeric string, or 'none'."""
    if value is None or value == "" or value == "none":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Kept as an alias since other modules may still reference the old name.
_parse_channel_id = _parse_id


def get_guild_config(db, guild_id: int) -> dict:
    """Fetches dynamic config from MongoDB. Falls back to defaults if not found."""
    config = db["bot_config"].find_one({"guild_id": guild_id}) if db is not None else {}
    if not config:
        config = {}

    builder_orders_id = _parse_id(config.get("BUILDER_ORDERS_CHANNEL_ID"))
    if builder_orders_id is None:
        builder_orders_id = _parse_id(os.getenv("BUILDER_ORDERS_CHANNEL_ID"))

    transcript_id = _parse_id(config.get("TRANSCRIPT_CHANNEL_ID"))
    if transcript_id is None:
        transcript_id = _parse_id(os.getenv("DEFAULT_TRANSCRIPT_CHANNEL_ID"))

    vouch_channel_id = _parse_id(config.get("VOUCH_CHANNEL_ID"))
    if vouch_channel_id is None:
        vouch_channel_id = _parse_id(os.getenv("DEFAULT_VOUCH_CHANNEL_ID"))

    return {
        "STAFF_ROLE_ID": _parse_id(config.get("STAFF_ROLE")),
        "MOD_ROLE_ID": _parse_id(config.get("MOD_ROLE")),
        "ADMIN_ROLE_ID": _parse_id(config.get("ADMIN_ROLE")),
        "TRUSTED_STAFF_ROLE_ID": _parse_id(config.get("TRUSTED_STAFF_ROLE")),
        "LOG_CHANNEL_ID": _parse_id(config.get("LOG_CHANNEL_ID")),
        "TRANSCRIPT_CHANNEL_ID": transcript_id,
        "BUILDER_ORDERS_CHANNEL_ID": builder_orders_id,
        "VOUCH_CHANNEL_ID": vouch_channel_id,
    }


def get_configured_role(guild: discord.Guild, cfg: dict, key: str) -> discord.Role | None:
    """key is one of STAFF_ROLE_ID / MOD_ROLE_ID / ADMIN_ROLE_ID / TRUSTED_STAFF_ROLE_ID."""
    role_id = cfg.get(key)
    return guild.get_role(role_id) if role_id else None


def member_has_role_id(member: discord.Member, role_id: int | None) -> bool:
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)


def member_has_config_role(member: discord.Member, cfg: dict, *keys: str) -> bool:
    """Checks whether member holds any of the given dashboard-configured roles
    (keys are STAFF_ROLE_ID / MOD_ROLE_ID / ADMIN_ROLE_ID / TRUSTED_STAFF_ROLE_ID)."""
    member_role_ids = {r.id for r in member.roles}
    for key in keys:
        role_id = cfg.get(key)
        if role_id and role_id in member_role_ids:
            return True
    return False


def member_has_role(member: discord.Member, role_name: str) -> bool:
    """Name-based role match — used for roles that are still configured by
    name (e.g. seller/builder roles), not the dashboard Staff/Mod/Admin roles."""
    return any(r.name == role_name for r in member.roles)

# ---------------------------------------------------------------------------
# Permission check helpers
# ---------------------------------------------------------------------------

def has_role(interaction: discord.Interaction, *role_names: str) -> bool:
    """Name-based match, used by the dashboard's per-command role overrides
    (command_perms), which are stored as role names."""
    user_roles = {r.name for r in interaction.user.roles}
    return bool(user_roles & set(role_names))

def is_admin_user(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return member_has_config_role(interaction.user, cfg, "ADMIN_ROLE_ID")

def is_mod_user(interaction: discord.Interaction) -> bool:
    if is_admin_user(interaction):
        return True
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return member_has_config_role(interaction.user, cfg, "STAFF_ROLE_ID", "MOD_ROLE_ID")

def is_staff_user(interaction: discord.Interaction) -> bool:
    if is_admin_user(interaction):
        return True
    cfg = get_guild_config(interaction.client.db, interaction.guild.id)
    return member_has_config_role(interaction.user, cfg, "STAFF_ROLE_ID")

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