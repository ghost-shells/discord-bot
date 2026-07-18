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


def run_agent_turn(guild_id: int, history: list, user_message: str, discord_api, db):
    """
    Runs one user turn to completion: repeatedly calls Groq, executing any
    read-only tool calls automatically, until the model either answers in
    plain text or proposes a destructive tool call.

    Returns:
      {
        "reply": str,                 # text to show the user
        "history": list,              # updated message history to persist client-side
        "pending_action": dict | None # {"tool": name, "args": {...}, "description": str} if confirmation needed
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

        # Handle the first tool call. If it's destructive, stop and ask for confirmation.
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if name in DESTRUCTIVE_TOOLS:
                pending = {
                    "tool": name,
                    "args": args,
                    "description": _friendly_action_description(name, args),
                }
                reply = choice.get("content") or f"I'd like to: {pending['description']}"
                return {"reply": reply, "history": messages[1:], "pending_action": pending}

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
