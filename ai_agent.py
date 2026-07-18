"""
ai_agent.py — Natural-language admin agent for the dashboard.

Uses Groq's free, OpenAI-compatible chat-completions API (tool calling
included) so no extra SDK is needed beyond `requests`, which app.py
already depends on.

How it fits together:
  - TOOLS / TOOL_FUNCTIONS define what the model can do. Every tool maps
    to a real Discord REST call made with the bot token, the same way
    app.py's existing `bot_console` form handlers already work.
  - Read-only tools (lookup_user, list_channels, list_roles,
    summarize_channel, list_warnings) execute immediately and their
    result is fed back to the model so it can keep reasoning.
  - Destructive tools (anything that changes the server) are NOT
    executed here. run_agent_turn() stops and hands the proposed call
    back to app.py, which shows the user a Confirm/Cancel step. Only
    app.py's /agent/execute route actually performs those.

app.py is expected to provide:
  - a `discord_api(method, path, reason=None, **kwargs)` callable
    (this is just _discord_api from app.py, passed in to avoid a
    circular import)
  - GROQ_API_KEY from the environment
"""

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

MAX_TOOL_ITERATIONS = 6  # safety cap so a confused model can't loop forever

SYSTEM_PROMPT = """You are the admin assistant for a Discord server's web dashboard.
Staff type natural-language requests and you either answer directly, call a
read-only tool to look something up, or propose a moderation/action tool call.

Rules:
- Always resolve a username to a numeric user_id via lookup_user before calling
  any tool that needs user_id. Never guess an ID.
- Keep replies short and concrete. State exactly what you're about to do before
  proposing a destructive action.
- If a request is ambiguous (e.g. which of several matching users), ask a short
  clarifying question instead of guessing.
- You cannot see channels/roles unless you call list_channels / list_roles, or
  they were already given to you in this conversation — don't assume IDs.
- Destructive tools are never executed by you directly; proposing the call is
  enough, the dashboard will ask the human to confirm.
"""

# ---------------------------------------------------------------------------
# Tool schema (OpenAI-compatible function-calling format, which Groq supports)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Search guild members by username/display name substring. Returns matching users with their numeric IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Username or partial username to search for"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_channels",
            "description": "List text channels in this guild with their IDs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_roles",
            "description": "List roles in this guild with their IDs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_channel",
            "description": "Fetch the most recent messages from a channel so you can summarize or analyze them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "How many recent messages to fetch (max 100)", "default": 30},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_warnings",
            "description": "List a member's stored moderation warnings.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    # ---- destructive: proposed only, executed after human confirms ----
    {
        "type": "function",
        "function": {
            "name": "kick_member",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_member",
            "description": "Ban a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "delete_days": {"type": "integer", "description": "Days of recent messages to delete (0-7)", "default": 0},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban_member",
            "description": "Remove a ban for a user ID.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeout_member",
            "description": "Apply a timeout (mute) to a member for a number of minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "minutes": {"type": "integer", "default": 10},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_timeout",
            "description": "Remove an active timeout from a member.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_role",
            "description": "Add a role to a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "role_id": {"type": "string"},
                },
                "required": ["user_id", "role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role",
            "description": "Remove a role from a member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "role_id": {"type": "string"},
                },
                "required": ["user_id", "role_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Post a message as the bot in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["channel_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dm_user",
            "description": "Send a direct message to a user as the bot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["user_id", "content"],
            },
        },
    },
]

READ_ONLY_TOOLS = {"lookup_user", "list_channels", "list_roles", "summarize_channel", "list_warnings"}
DESTRUCTIVE_TOOLS = {t["function"]["name"] for t in TOOLS} - READ_ONLY_TOOLS


def _friendly_action_description(tool_name: str, args: dict) -> str:
    """Human-readable one-liner shown in the Confirm/Cancel UI."""
    uid = args.get("user_id", "?")
    if tool_name == "kick_member":
        return f"Kick user `{uid}` — reason: {args.get('reason') or 'none given'}"
    if tool_name == "ban_member":
        return f"Ban user `{uid}` (delete {args.get('delete_days', 0)}d of messages) — reason: {args.get('reason') or 'none given'}"
    if tool_name == "unban_member":
        return f"Unban user `{uid}`"
    if tool_name == "timeout_member":
        return f"Timeout user `{uid}` for {args.get('minutes', 10)} minute(s) — reason: {args.get('reason') or 'none given'}"
    if tool_name == "remove_timeout":
        return f"Remove timeout from user `{uid}`"
    if tool_name == "add_role":
        return f"Add role `{args.get('role_id')}` to user `{uid}`"
    if tool_name == "remove_role":
        return f"Remove role `{args.get('role_id')}` from user `{uid}`"
    if tool_name == "send_message":
        return f"Send message to channel `{args.get('channel_id')}`: \"{(args.get('content') or '')[:80]}\""
    if tool_name == "dm_user":
        return f"DM user `{uid}`: \"{(args.get('content') or '')[:80]}\""
    return f"{tool_name}({args})"


# ---------------------------------------------------------------------------
# Read-only tool implementations — need guild_id + the discord_api callable
# ---------------------------------------------------------------------------

def _run_read_only_tool(tool_name: str, args: dict, guild_id: int, discord_api, db):
    try:
        if tool_name == "lookup_user":
            query = (args.get("query") or "").lower()
            members = []
            after = None
            for _ in range(5):  # up to ~500 members scanned, matches console_lookup_user's spirit
                params = {"limit": 1000}
                if after:
                    params["after"] = after
                r = discord_api("GET", f"/guilds/{guild_id}/members", params=params)
                if not r.ok:
                    break
                page = r.json()
                if not page:
                    break
                members.extend(page)
                if len(page) < 1000:
                    break
                after = page[-1]["user"]["id"]

            matches = []
            for m in members:
                user = m.get("user", {})
                uname = user.get("username", "")
                nick = m.get("nick") or user.get("global_name") or uname
                if query in uname.lower() or query in nick.lower():
                    matches.append({"id": user.get("id"), "username": uname, "display_name": nick})
            return {"matches": matches[:15]}

        if tool_name == "list_channels":
            r = discord_api("GET", f"/guilds/{guild_id}/channels")
            if not r.ok:
                return {"error": "Could not fetch channels"}
            chans = [{"id": c["id"], "name": c["name"]} for c in r.json() if c.get("type") == 0]
            return {"channels": chans}

        if tool_name == "list_roles":
            r = discord_api("GET", f"/guilds/{guild_id}/roles")
            if not r.ok:
                return {"error": "Could not fetch roles"}
            roles = [{"id": ro["id"], "name": ro["name"]} for ro in r.json() if ro["name"] != "@everyone"]
            return {"roles": roles}

        if tool_name == "summarize_channel":
            channel_id = args.get("channel_id")
            limit = max(1, min(100, int(args.get("limit", 30))))
            r = discord_api("GET", f"/channels/{channel_id}/messages", params={"limit": limit})
            if not r.ok:
                return {"error": "Could not fetch messages — check the bot can view that channel."}
            msgs = r.json()
            simplified = [
                {"author": m.get("author", {}).get("username", "?"), "content": m.get("content", "")}
                for m in reversed(msgs)
            ]
            return {"messages": simplified}

        if tool_name == "list_warnings":
            if db is None:
                return {"error": "Database unavailable"}
            user_id = args.get("user_id")
            doc = db["warnings"].find_one({"guild_id": guild_id, "user_id": int(user_id)})
            return {"warnings": doc.get("warnings", []) if doc else []}

    except Exception as e:
        logger.error(f"Agent read-only tool '{tool_name}' failed: {e}")
        return {"error": str(e)[:300]}

    return {"error": f"Unknown read-only tool {tool_name}"}


# ---------------------------------------------------------------------------
# Groq call
# ---------------------------------------------------------------------------

def _call_groq(messages):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
    }
    resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def simple_chat(messages: list, temperature: float = 0.7, max_tokens: int = 600) -> str:
    """Plain conversational completion — no tools. Used by cogs/ai_chat.py for
    @mention / reply-to-bot conversations in Discord itself (not the dashboard
    agent, which uses run_agent_turn below)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"] or "..."


def _run_destructive_tool(tool_name: str, args: dict, guild_id: int, discord_api):
    """Actually performs a destructive action via the Discord REST API.
    Shared by app.py's dashboard confirm-execute route and the trusted-staff
    auto-execute path used from Discord chat (cogs/ai_chat.py)."""
    from datetime import datetime, timedelta  # local import to avoid a hard dep for read-only-only callers

    user_id = str(args.get("user_id", "") or "")
    reason = (args.get("reason") or "Requested via AI").strip()
    ok, error, detail = False, None, ""

    try:
        if tool_name == "kick_member":
            r = discord_api("DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason)
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = reason

        elif tool_name == "ban_member":
            delete_days = max(0, min(7, int(args.get("delete_days", 0) or 0)))
            r = discord_api(
                "PUT", f"/guilds/{guild_id}/bans/{user_id}",
                reason=reason, json={"delete_message_seconds": delete_days * 86400},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = reason

        elif tool_name == "unban_member":
            r = discord_api("DELETE", f"/guilds/{guild_id}/bans/{user_id}")
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"

        elif tool_name == "timeout_member":
            minutes = max(1, min(40320, int(args.get("minutes", 10) or 10)))
            until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat() + "Z"
            r = discord_api(
                "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                reason=reason, json={"communication_disabled_until": until},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"{minutes}m — {reason}"

        elif tool_name == "remove_timeout":
            r = discord_api(
                "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                json={"communication_disabled_until": None},
            )
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"

        elif tool_name in ("add_role", "remove_role"):
            role_id = args.get("role_id")
            method = "PUT" if tool_name == "add_role" else "DELETE"
            r = discord_api(method, f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"role {role_id}"

        elif tool_name == "send_message":
            channel_id = args.get("channel_id")
            content = (args.get("content") or "").strip()[:2000]
            r = discord_api("POST", f"/channels/{channel_id}/messages", json={"content": content})
            ok = r.ok
            error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = f"channel #{channel_id}: {content[:80]}"

        elif tool_name == "dm_user":
            content = (args.get("content") or "").strip()[:2000]
            dm = discord_api("POST", "/users/@me/channels", json={"recipient_id": user_id})
            if not dm.ok:
                ok, error = False, f"HTTP {dm.status_code}: {dm.text[:200]}"
            else:
                dm_channel_id = dm.json()["id"]
                r = discord_api("POST", f"/channels/{dm_channel_id}/messages", json={"content": content})
                ok = r.ok
                error = None if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            detail = content[:80]

        else:
            error = "Unknown action."

    except Exception as e:
        logger.error(f"_run_destructive_tool '{tool_name}' failed: {e}")
        ok, error = False, str(e)[:300]

    return ok, error, detail


def run_agent_turn(
    guild_id: int,
    history: list,
    user_message: str,
    discord_api,
    db,
    auto_execute: bool = False,
    log_action=None,
):
    """
    Runs one user turn to completion: repeatedly calls Groq, executing any
    read-only tool calls automatically.

    - auto_execute=False (default; used by any confirm-first caller):
      stops and returns a "pending_action" the first time the model wants
      to call a destructive tool, without running it.
    - auto_execute=True (used by trusted-staff Discord chat, which already
      gated the whole request on the Trusted Staff role): destructive tools
      are executed immediately via discord_api, their result is fed back to
      the model, and the loop continues — no separate confirmation step.
      If log_action(tool_name, args, ok, error, detail) is given, it's
      called after each auto-executed action for audit logging.

    Returns:
      {
        "reply": str,
        "history": list,
        "pending_action": dict | None   # only ever set when auto_execute=False
      }
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_message}
    ]

    for _ in range(MAX_TOOL_ITERATIONS):
        data = _call_groq(messages)
        choice = data["choices"][0]["message"]
        messages.append(choice)

        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            reply = choice.get("content") or "..."
            return {"reply": reply, "history": messages[1:], "pending_action": None}

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if name in DESTRUCTIVE_TOOLS and not auto_execute:
                pending = {
                    "tool": name,
                    "args": args,
                    "description": _friendly_action_description(name, args),
                }
                reply = choice.get("content") or f"I'd like to: {pending['description']}"
                return {"reply": reply, "history": messages[1:], "pending_action": pending}

            if name in DESTRUCTIVE_TOOLS:  # auto_execute is True here
                ok, error, detail = _run_destructive_tool(name, args, guild_id, discord_api)
                if log_action:
                    try:
                        log_action(name, args, ok, error, detail)
                    except Exception as e:
                        logger.error(f"log_action callback failed: {e}")
                result = {"ok": ok, "error": error} if not ok else {"ok": True, "detail": detail}
            else:
                result = _run_read_only_tool(name, args, guild_id, discord_api, db)

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": name,
                "content": json.dumps(result)[:4000],
            })

    return {
        "reply": "I wasn't able to finish that within a reasonable number of steps — try breaking it into a smaller request.",
        "history": messages[1:],
        "pending_action": None,
    }
