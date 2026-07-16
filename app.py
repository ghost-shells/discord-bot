# app.py – full file with payment log channel added

import os
import time
from flask import Flask, render_template, redirect, request, session, Response, jsonify
import requests
from dotenv import load_dotenv
from db import get_bot_token, get_db, test_mongodb
from bson.objectid import ObjectId
from flask import abort
from datetime import datetime, timedelta
import logging

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
flask_secret = os.getenv("FLASK_SECRET")
if not flask_secret:
    raise RuntimeError("FLASK_SECRET environment variable must be set!")
app.secret_key = flask_secret

# Force Flask to use secure cookies for HTTPS (Railway)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Keep users logged in across browser restarts until they explicitly log out.
# Without this, Flask issues a browser-session-only cookie that gets wiped
# whenever the browser fully closes, forcing a re-login even though the
# Discord authorization itself is still valid.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Discord OAuth2 Config
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
BOT_TOKEN = get_bot_token()

# Connect to MongoDB (shared singleton with the bot)
db = get_db()

# Test MongoDB connection on startup
if db is not None:
    logger.info("🔍 Testing MongoDB connection...")
    if test_mongodb():
        logger.info("✅ MongoDB test passed!")
    else:
        logger.error("❌ MongoDB test failed!")
else:
    logger.error("❌ Could not connect to MongoDB at startup!")


# ── Track which guilds the bot is actually a member of ──────────────────────
# The OAuth "guilds" scope only tells us which servers the *user* manages —
# it says nothing about whether the bot itself has been added. We fetch the
# bot's own guild list (Bot token) and cache it briefly so we're not hitting
# Discord on every page load.
_bot_guild_ids_cache = {"ids": set(), "ts": 0}
_BOT_GUILDS_CACHE_TTL = 60  # seconds


def get_bot_guild_ids():
    """Return the set of guild IDs the bot is currently a member of."""
    now = time.time()
    if _bot_guild_ids_cache["ids"] and (now - _bot_guild_ids_cache["ts"] < _BOT_GUILDS_CACHE_TTL):
        return _bot_guild_ids_cache["ids"]

    if not BOT_TOKEN:
        return _bot_guild_ids_cache["ids"]

    headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    ids = set()
    after = None
    try:
        while True:
            params = {"limit": 200}
            if after:
                params["after"] = after
            resp = requests.get(
                "https://discord.com/api/v10/users/@me/guilds",
                headers=headers, params=params, timeout=10,
            )
            if not resp.ok:
                break
            page = resp.json()
            if not page:
                break
            ids.update(int(g["id"]) for g in page)
            if len(page) < 200:
                break
            after = page[-1]["id"]
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch bot guild list: {e}")
        if _bot_guild_ids_cache["ids"]:
            return _bot_guild_ids_cache["ids"]  # serve stale cache over nothing

    _bot_guild_ids_cache["ids"] = ids
    _bot_guild_ids_cache["ts"] = now
    return ids


def bot_in_guild(guild_id) -> bool:
    return int(guild_id) in get_bot_guild_ids()


@app.route("/")
def index():
    if "access_token" in session:
        return redirect("/dashboard")
    return render_template("index.html", client_id=CLIENT_ID, redirect_uri=REDIRECT_URI)


@app.route("/login")
def login():
    if "access_token" in session:
        return redirect("/dashboard")
    return redirect(
        f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify%20guilds"
    )


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": "identify guilds",
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DashboardBot/1.0",
    }

    response = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    tokens = response.json()

    if "access_token" not in tokens:
        return redirect("/")

    # Make this a long-lived session (see PERMANENT_SESSION_LIFETIME above)
    # instead of a browser-session-only cookie.
    session.permanent = True
    session["access_token"] = tokens["access_token"]

    # Store which Discord user this is, so per-user features (like the MC
    # account link) know whose session to scope requests to.
    me_response = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {session['access_token']}", "User-Agent": "DashboardBot/1.0"},
    )
    me = me_response.json()
    if isinstance(me, dict) and "id" in me:
        session["discord_user"] = {
            "id": me["id"],
            "username": me.get("username"),
            "avatar": me.get("avatar"),
        }

    guild_response = requests.get(
        "https://discord.com/api/users/@me/guilds",
        headers={"Authorization": f"Bearer {session['access_token']}", "User-Agent": "DashboardBot/1.0"},
    )
    guilds = guild_response.json()

    if not isinstance(guilds, list):
        return redirect("/")

    manageable_guilds = [
        g
        for g in guilds
        if (int(g.get("permissions", 0)) & 0x8) == 0x8 or (int(g.get("permissions", 0)) & 0x20) == 0x20
    ]
    session["guilds"] = manageable_guilds
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    if "access_token" not in session:
        return redirect("/")

    bot_guild_ids = get_bot_guild_ids()
    guilds = session.get("guilds", [])
    for g in guilds:
        g["bot_present"] = int(g["id"]) in bot_guild_ids

    return render_template(
        "dashboard.html",
        guilds=guilds,
        bot_missing=request.args.get("bot_missing"),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/transcripts")
def transcripts():
    """List all transcripts for the user's guilds."""
    if "access_token" not in session:
        return redirect("/")

    if db is None:
        return "<h1>Error: Database connection not available!</h1>", 500

    guilds = session.get("guilds", [])

    transcripts_by_guild = {}
    for guild in guilds:
        guild_id = int(guild["id"])
        guild_transcripts = list(db["transcripts"].find({"guild_id": guild_id}).sort("closed_at", -1).limit(50))

        if guild_transcripts:
            transcripts_by_guild[guild["name"]] = {"id": guild_id, "transcripts": guild_transcripts}

    return render_template("transcripts_list.html", transcripts_by_guild=transcripts_by_guild)


@app.route("/transcripts/<transcript_id>")
def view_transcript(transcript_id):
    """View a specific HTML transcript."""
    if "access_token" not in session:
        return redirect("/")

    if db is None:
        abort(500, "Database connection not available")

    try:
        obj_id = ObjectId(transcript_id)
    except:
        abort(404)

    transcript = db["transcripts"].find_one({"_id": obj_id})
    if not transcript:
        abort(404)

    guilds = session.get("guilds", [])
    user_guild_ids = [int(g["id"]) for g in guilds]

    if transcript["guild_id"] not in user_guild_ids:
        abort(403)

    return transcript["html_content"]


@app.route("/transcripts/<transcript_id>/raw")
def view_transcript_raw(transcript_id):
    """Download raw HTML transcript."""
    if "access_token" not in session:
        return redirect("/")

    if db is None:
        abort(500, "Database connection not available")

    try:
        obj_id = ObjectId(transcript_id)
    except:
        abort(404)

    transcript = db["transcripts"].find_one({"_id": obj_id})
    if not transcript:
        abort(404)

    guilds = session.get("guilds", [])
    user_guild_ids = [int(g["id"]) for g in guilds]

    if transcript["guild_id"] not in user_guild_ids:
        abort(403)

    response = Response(transcript["html_content"], mimetype="text/html")
    response.headers["Content-Disposition"] = f"attachment; filename=transcript-{transcript['channel_name']}.html"
    return response


@app.route("/dashboard/<int:guild_id>", methods=["GET", "POST"])
def guild_dashboard(guild_id):
    if "access_token" not in session:
        return redirect("/")

    # Verify the logged-in user actually has access to this guild
    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)

    # The bot itself must be in the server before there's anything to configure
    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")

    if db is None:
        return "<h1>Error: Database connection not available!</h1>", 500

    # Handle Saving Settings
    if request.method == "POST":
        form_type = request.form.get("form_type")

        if form_type == "autorole":
            role_id = request.form.get("autorole_id")
            if role_id and role_id != "none":
                db["autorole_settings"].update_one(
                    {"guild_id": guild_id}, {"$set": {"role_id": int(role_id)}}, upsert=True
                )
            elif role_id == "none":
                db["autorole_settings"].delete_one({"guild_id": guild_id})

        elif form_type == "welcome":
            channel_id = request.form.get("welcome_channel_id")
            message = request.form.get("welcome_message")
            if channel_id and channel_id != "none":
                db["welcome_settings"].update_one(
                    {"guild_id": guild_id},
                    {"$set": {"channel_id": int(channel_id), "message": message}},
                    upsert=True,
                )
            elif channel_id == "none":
                db["welcome_settings"].delete_one({"guild_id": guild_id})

        elif form_type == "logging":
            channel_id = request.form.get("log_channel_id")
            transcript_channel_id = request.form.get("TRANSCRIPT_CHANNEL_ID")

            if channel_id and channel_id != "none":
                db["log_settings"].update_one(
                    {"guild_id": guild_id}, {"$set": {"channel_id": int(channel_id)}}, upsert=True
                )
            elif channel_id == "none":
                db["log_settings"].delete_one({"guild_id": guild_id})

            db["bot_config"].update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "TRANSCRIPT_CHANNEL_ID": int(transcript_channel_id)
                        if transcript_channel_id and transcript_channel_id != "none"
                        else None
                    }
                },
                upsert=True,
            )

        elif form_type == "automod":
            block_links = request.form.get("block_links") == "on"
            block_invites = request.form.get("block_invites") == "on"
            banned_words = [w.strip() for w in request.form.get("banned_words", "").split(",") if w.strip()]
            active_channels = request.form.getlist("automod_channels")
            active_channels = [int(c) for c in active_channels]

            db["automod_settings"].update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "block_links": block_links,
                        "block_invites": block_invites,
                        "banned_words": banned_words,
                        "active_channels": active_channels,
                    }
                },
                upsert=True,
            )

        elif form_type == "announcement":
            channel_id = request.form.get("announcement_channel_id")
            message = request.form.get("announcement_message")
            if channel_id and message:
                requests.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"},
                    json={"content": message},
                )

        elif form_type == "config":
            staff_role = request.form.get("STAFF_ROLE")
            mod_role = request.form.get("MOD_ROLE")
            admin_role = request.form.get("ADMIN_ROLE")
            trusted_staff_role = request.form.get("TRUSTED_STAFF_ROLE")
            log_channel_id = request.form.get("LOG_CHANNEL_ID")
            transcript_channel_id = request.form.get("TRANSCRIPT_CHANNEL_ID")
            builder_orders_channel_id = request.form.get("BUILDER_ORDERS_CHANNEL_ID")
            vouch_channel_id = request.form.get("VOUCH_CHANNEL_ID")

            db["bot_config"].update_one(
                {"guild_id": guild_id},
                {
                    "$set": {
                        "STAFF_ROLE": staff_role,
                        "MOD_ROLE": mod_role,
                        "ADMIN_ROLE": admin_role,
                        "TRUSTED_STAFF_ROLE": trusted_staff_role,
                        "LOG_CHANNEL_ID": int(log_channel_id) if log_channel_id and log_channel_id != "none" else None,
                        "TRANSCRIPT_CHANNEL_ID": int(transcript_channel_id)
                        if transcript_channel_id and transcript_channel_id != "none"
                        else None,
                        "BUILDER_ORDERS_CHANNEL_ID": int(builder_orders_channel_id)
                        if builder_orders_channel_id and builder_orders_channel_id != "none"
                        else None,
                        "VOUCH_CHANNEL_ID": int(vouch_channel_id)
                        if vouch_channel_id and vouch_channel_id != "none"
                        else None,
                    }
                },
                upsert=True,
            )
        elif form_type == "create_app":
            app_id = request.form.get("app_id").lower().replace(" ", "-")
            app_name = request.form.get("app_name")
            questions_raw = request.form.get("questions_text", "")
            questions = [q.strip() for q in questions_raw.split("\n") if q.strip()]
            is_open = request.form.get("is_open") == "on"
            submitted_channel_id = request.form.get("submitted_channel_id")
            accepted_channel_id = request.form.get("accepted_channel_id")
            denied_channel_id = request.form.get("denied_channel_id")

            if app_id and app_name and questions:
                db["applications_config"].update_one(
                    {"guild_id": guild_id, "app_id": app_id},
                    {
                        "$set": {
                            "app_name": app_name,
                            "questions": questions,
                            "is_open": is_open,
                            "submitted_channel_id": int(submitted_channel_id)
                            if submitted_channel_id and submitted_channel_id != "none"
                            else None,
                            "accepted_channel_id": int(accepted_channel_id)
                            if accepted_channel_id and accepted_channel_id != "none"
                            else None,
                            "denied_channel_id": int(denied_channel_id)
                            if denied_channel_id and denied_channel_id != "none"
                            else None,
                        }
                    },
                    upsert=True,
                )

        elif form_type == "send_app_panel":
            app_id = request.form.get("panel_app_id")
            panel_channel_id = request.form.get("panel_channel_id")

            app_config = db["applications_config"].find_one({"guild_id": guild_id, "app_id": app_id})
            if app_config and panel_channel_id:
                component = {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "label": f"Apply for {app_config['app_name']}",
                            "style": 1,
                            "custom_id": f"apply_{app_id}",
                            "emoji": {"name": "📝"},
                        }
                    ],
                }
                embed_payload = {
                    "title": f"📝 {app_config['app_name']}",
                    "description": "Click the button below to start your application. You will receive a DM from the bot to fill out the questions.",
                    "color": 0x5865F2,
                    "footer": {"text": f"App ID: {app_id}"},
                }
                requests.post(
                    f"https://discord.com/api/v10/channels/{panel_channel_id}/messages",
                    headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"},
                    json={"embeds": [embed_payload], "components": [component]},
                )

        elif form_type == "building_config":
            t1 = request.form.get("BUILDER_T1_ROLE_ID")
            t2 = request.form.get("BUILDER_T2_ROLE_ID")
            t3 = request.form.get("BUILDER_T3_ROLE_ID")
            ticket_ping = request.form.get("BUILD_TICKET_PING_ROLE_ID")
            order_ping = request.form.get("BUILD_ORDER_PING_ROLE_ID")
            payment_method = request.form.get("PAYMENT_METHOD", "")
            payment_receiver_ign = request.form.get("PAYMENT_RECEIVER_IGN", "").strip()
            # NEW: payment log channel
            payment_log_channel_id = request.form.get("PAYMENT_LOG_CHANNEL_ID")

            db["bot_config"].update_one(
                {"guild_id": guild_id},
                {"$set": {
                    "BUILDER_T1_ROLE_ID": int(t1) if t1 and t1 != "none" else None,
                    "BUILDER_T2_ROLE_ID": int(t2) if t2 and t2 != "none" else None,
                    "BUILDER_T3_ROLE_ID": int(t3) if t3 and t3 != "none" else None,
                    "BUILD_TICKET_PING_ROLE_ID": int(ticket_ping) if ticket_ping and ticket_ping != "none" else None,
                    "BUILD_ORDER_PING_ROLE_ID": int(order_ping) if order_ping and order_ping != "none" else None,
                    "PAYMENT_METHOD": payment_method.strip() if payment_method else "",
                    "PAYMENT_RECEIVER_IGN": payment_receiver_ign if payment_receiver_ign else None,
                    "PAYMENT_LOG_CHANNEL_ID": int(payment_log_channel_id) if payment_log_channel_id and payment_log_channel_id != "none" else None,
                }},
                upsert=True
            )
        elif form_type == "add_build":
            build_name = request.form.get("build_name")
            build_price = request.form.get("build_price")
            build_desc = request.form.get("build_desc", "")
            build_emoji = request.form.get("build_emoji", "🧱")
            if build_name and build_price:
                builds_doc = db["building_panels"].find_one({"guild_id": guild_id})
                new_build = {
                    "id": build_name.lower().replace(" ", "_"),
                    "name": build_name,
                    "price": build_price,
                    "description": build_desc,
                    "emoji": build_emoji
                }
                if builds_doc:
                    if not any(b["id"] == new_build["id"] for b in builds_doc.get("builds", [])):
                        db["building_panels"].update_one(
                            {"guild_id": guild_id},
                            {"$push": {"builds": new_build}}
                        )
                else:
                    db["building_panels"].insert_one({"guild_id": guild_id, "builds": [new_build]})
        elif form_type == "update_build":
            build_id = request.form.get("edit_build_id")
            build_name = request.form.get("build_name")
            build_price = request.form.get("build_price")
            build_desc = request.form.get("build_desc", "")
            build_emoji = request.form.get("build_emoji", "🧱")
            if build_id and build_name and build_price:
                db["building_panels"].update_one(
                    {"guild_id": guild_id, "builds.id": build_id},
                    {"$set": {
                        "builds.$.name": build_name,
                        "builds.$.price": build_price,
                        "builds.$.description": build_desc,
                        "builds.$.emoji": build_emoji
                    }}
                )
        elif form_type == "delete_build":
            build_id = request.form.get("delete_build_id")
            if build_id:
                db["building_panels"].update_one(
                    {"guild_id": guild_id},
                    {"$pull": {"builds": {"id": build_id}}}
                )

        elif form_type == "delete_app":
            app_id = request.form.get("delete_app_id")
            if app_id:
                db["applications_config"].delete_one({"guild_id": guild_id, "app_id": app_id})

        return redirect(f"/dashboard/{guild_id}")

    # GET Request: Fetch Data for Display
    if not BOT_TOKEN:
        return "<h1>Error: DISCORD_BOT_TOKEN or DISCORD_TOKEN is missing from environment variables!</h1>", 500

    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}

    # Fetch Roles
    roles_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/roles", headers=bot_headers)
    if roles_res.status_code != 200:
        logger.error(f"❌ ROLES FETCH FAILED: {roles_res.status_code} - {roles_res.text}")
    roles = roles_res.json() if roles_res.status_code == 200 else []
    roles = [r for r in roles if r["name"] != "@everyone" and not r["managed"]]

    # Fetch Channels
    chans_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=bot_headers)
    if chans_res.status_code != 200:
        logger.error(f"❌ CHANNELS FETCH FAILED: {chans_res.status_code} - {chans_res.text}")
    channels = chans_res.json() if chans_res.status_code == 200 else []
    text_channels = [c for c in channels if c["type"] == 0]

    # Fetch Settings
    settings = {
        "autorole": db["autorole_settings"].find_one({"guild_id": guild_id}),
        "welcome": db["welcome_settings"].find_one({"guild_id": guild_id}),
        "logging": db["log_settings"].find_one({"guild_id": guild_id}),
        "automod": db["automod_settings"].find_one({"guild_id": guild_id}),
        "config": (
            lambda cfg: {
                "STAFF_ROLE": cfg.get("STAFF_ROLE", "Staff"),
                "MOD_ROLE": cfg.get("MOD_ROLE", "Moderator"),
                "ADMIN_ROLE": cfg.get("ADMIN_ROLE", "Admin"),
                "TRUSTED_STAFF_ROLE": cfg.get("TRUSTED_STAFF_ROLE", "Trusted Staff"),
                "LOG_CHANNEL_ID": cfg.get("LOG_CHANNEL_ID"),
                "TRANSCRIPT_CHANNEL_ID": cfg.get("TRANSCRIPT_CHANNEL_ID"),
                "BUILDER_ORDERS_CHANNEL_ID": cfg.get("BUILDER_ORDERS_CHANNEL_ID"),
                "VOUCH_CHANNEL_ID": cfg.get("VOUCH_CHANNEL_ID"),
            }
        )(db["bot_config"].find_one({"guild_id": guild_id}) or {}),
        "applications": list(db["applications_config"].find({"guild_id": guild_id})),
        "command_perms": {doc["command_name"]: doc["roles"] for doc in db["command_perms"].find({"guild_id": guild_id})},
        "building": {
            "config": db["bot_config"].find_one({"guild_id": guild_id}) or {},
            "builds": (db["building_panels"].find_one({"guild_id": guild_id}) or {}).get("builds", [])
        },
    }

    guild_name = "Unknown Server"
    for g in session.get("guilds", []):
        if int(g["id"]) == guild_id:
            guild_name = g["name"]
            break

    return render_template(
        "settings.html", guild_id=guild_id, guild_name=guild_name, roles=roles, channels=text_channels, settings=settings
    )


@app.route("/dashboard/<int:guild_id>/commands", methods=["GET", "POST"])
def commands_dashboard(guild_id):
    """Command permissions management page."""
    if "access_token" not in session:
        return redirect("/")

    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)

    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")

    if db is None:
        logger.error("❌ [CMD_PERMS] db is None — MongoDB never connected!")
        if request.method == "POST":
            return jsonify({"success": False, "error": "Database connection not available"}), 500
        return "<h1>Error: Database connection not available!</h1>", 500

    # ------------------------------------------------------------------ POST
    if request.method == "POST":
        form_type = request.form.get("form_type")
        logger.info(f"[CMD_PERMS] POST received | guild={guild_id} | form_type={form_type!r}")

        if form_type != "save_cmd_perms":
            logger.warning(f"[CMD_PERMS] Unknown form_type: {form_type!r}")
            return jsonify({"success": False, "error": f"Unknown form_type: {form_type}"}), 400

        # --- log every raw form field so we can see exactly what arrived ----
        logger.info("[CMD_PERMS] ---- RAW FORM DUMP START ----")
        for k in request.form.keys():
            logger.info(f"[CMD_PERMS]   {k!r} => {request.form.getlist(k)!r}")
        logger.info("[CMD_PERMS] ---- RAW FORM DUMP END ----")

        # --- check db is still alive before touching it --------------------
        try:
            db.command("ping")
            logger.info("[CMD_PERMS] ✅ MongoDB ping OK before save")
        except Exception as ping_err:
            logger.error(f"[CMD_PERMS] ❌ MongoDB ping FAILED: {ping_err}")
            return jsonify({"success": False, "error": f"MongoDB unreachable: {ping_err}"}), 500

        try:
            saved_commands = []
            seen_commands = set()

            for key in request.form.keys():
                if not key.startswith("has_cmd_"):
                    continue

                command_name = key[8:]  # strip "has_cmd_"

                # MultiDict.keys() can yield the same key more than once
                if command_name in seen_commands:
                    logger.warning(f"[CMD_PERMS] Duplicate key skipped: {command_name!r}")
                    continue
                seen_commands.add(command_name)

                raw_roles = request.form.getlist(f"cmd_{command_name}")
                roles = [r.strip() for r in raw_roles if r and r.strip()]

                logger.info(f"[CMD_PERMS] Command {command_name!r} | raw_roles={raw_roles!r} | clean_roles={roles!r}")

                if roles:
                    try:
                        result = db["command_perms"].update_one(
                            {"guild_id": guild_id, "command_name": command_name},
                            {"$set": {
                                "guild_id": guild_id,
                                "command_name": command_name,
                                "roles": roles
                            }},
                            upsert=True
                        )
                        logger.info(
                            f"[CMD_PERMS] UPSERT {command_name!r} | "
                            f"matched={result.matched_count} modified={result.modified_count} "
                            f"upserted_id={result.upserted_id}"
                        )

                        # immediate read-back to confirm it landed
                        verify = db["command_perms"].find_one(
                            {"guild_id": guild_id, "command_name": command_name}
                        )
                        if verify is None:
                            logger.error(f"[CMD_PERMS] ❌ READ-BACK FAILED for {command_name!r} — doc not found!")
                        elif verify.get("roles") != roles:
                            logger.error(
                                f"[CMD_PERMS] ❌ READ-BACK MISMATCH for {command_name!r} | "
                                f"expected={roles!r} got={verify.get('roles')!r}"
                            )
                        else:
                            logger.info(f"[CMD_PERMS] ✅ Read-back OK for {command_name!r}: {roles!r}")

                        saved_commands.append(command_name)

                    except Exception as db_err:
                        logger.error(f"[CMD_PERMS] ❌ DB error upserting {command_name!r}: {db_err}", exc_info=True)
                        raise

                else:
                    # no roles selected → delete the document
                    try:
                        result = db["command_perms"].delete_one(
                            {"guild_id": guild_id, "command_name": command_name}
                        )
                        logger.info(f"[CMD_PERMS] DELETE {command_name!r} | deleted_count={result.deleted_count}")

                        verify = db["command_perms"].find_one(
                            {"guild_id": guild_id, "command_name": command_name}
                        )
                        if verify is None:
                            logger.info(f"[CMD_PERMS] ✅ Deletion confirmed for {command_name!r}")
                        else:
                            logger.error(f"[CMD_PERMS] ❌ Document still present after delete for {command_name!r}!")

                        saved_commands.append(command_name)

                    except Exception as db_err:
                        logger.error(f"[CMD_PERMS] ❌ DB error deleting {command_name!r}: {db_err}", exc_info=True)
                        raise

            # final state dump
            all_saved = list(db["command_perms"].find({"guild_id": guild_id}))
            logger.info(f"[CMD_PERMS] ---- FINAL DB STATE ({len(all_saved)} docs) ----")
            for doc in all_saved:
                logger.info(f"[CMD_PERMS]   /{doc['command_name']} => {doc['roles']!r}")
            logger.info("[CMD_PERMS] ---- END FINAL DB STATE ----")

            logger.info(f"[CMD_PERMS] ✅ Done. Processed {len(saved_commands)} command(s): {saved_commands}")
            return jsonify({
                "success": True,
                "message": f"Saved permissions for {len(saved_commands)} command(s)",
                "saved": saved_commands
            })

        except Exception as e:
            logger.error(f"[CMD_PERMS] ❌ Unhandled exception during save: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------ GET
    if not BOT_TOKEN:
        return "<h1>Error: Discord bot token missing!</h1>", 500

    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}

    # Fetch roles from Discord
    try:
        roles_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/roles", headers=bot_headers)
        logger.info(f"[CMD_PERMS] Discord roles fetch | status={roles_res.status_code}")
        if roles_res.status_code == 200:
            roles = [
                {"id": str(r["id"]), "name": r["name"], "color": r["color"]}
                for r in roles_res.json()
                if r["name"] != "@everyone" and not r["managed"]
            ]
            logger.info(f"[CMD_PERMS] Fetched {len(roles)} roles: {[r['name'] for r in roles]}")
        else:
            logger.error(f"[CMD_PERMS] ❌ Discord roles fetch failed: {roles_res.text}")
            roles = []
    except Exception as e:
        logger.error(f"[CMD_PERMS] ❌ Exception fetching roles: {e}", exc_info=True)
        roles = []

    # Fetch saved command permissions from MongoDB
    command_perms = {}
    try:
        # ping first so we know if db is reachable
        db.command("ping")
        logger.info("[CMD_PERMS] ✅ MongoDB ping OK on GET")

        all_docs = list(db["command_perms"].find({"guild_id": guild_id}))
        logger.info(f"[CMD_PERMS] Loaded {len(all_docs)} permission docs for guild {guild_id}")
        for doc in all_docs:
            command_perms[doc["command_name"]] = doc["roles"]
            logger.info(f"[CMD_PERMS]   /{doc['command_name']} => {doc['roles']!r}")

        if not all_docs:
            logger.info("[CMD_PERMS] No saved permissions found for this guild")

    except Exception as e:
        logger.error(f"[CMD_PERMS] ❌ Exception reading from MongoDB: {e}", exc_info=True)

    guild_name = "Unknown Server"
    for g in session.get("guilds", []):
        if int(g["id"]) == guild_id:
            guild_name = g["name"]
            break

    settings = {"command_perms": command_perms}
    logger.info(f"[CMD_PERMS] Rendering commands.html | guild={guild_name} | perms_count={len(command_perms)}")

    return render_template(
        "commands.html",
        guild_id=guild_id,
        guild_name=guild_name,
        roles=roles,
        settings=settings,
        timestamp=datetime.now().timestamp()
    )


## ─────────────────────────────────────────────────────────────────────────
## Build Tracker Dashboard (unchanged)
## ─────────────────────────────────────────────────────────────────────────

@app.route("/dashboard/<int:guild_id>/builds")
def builds_dashboard(guild_id):
    """Build tracker: active orders + builder stats."""
    if "access_token" not in session:
        return redirect("/")

    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)

    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")

    if db is None:
        return "<h1>Error: Database connection not available!</h1>", 500

    guilds = session.get("guilds", [])
    if not any(int(g["id"]) == guild_id for g in guilds):
        abort(403)

    guild_name = next((g["name"] for g in guilds if int(g["id"]) == guild_id), "Unknown Server")

    all_orders = list(db["building_orders"].find({"guild_id": guild_id}))

    # Resolve Discord usernames via bot token
    user_cache = {}
    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}

    def resolve_user(user_id):
        if not user_id:
            return "Unknown"
        if user_id in user_cache:
            return user_cache[user_id]
        try:
            r = requests.get(
                f"https://discord.com/api/v10/users/{user_id}",
                headers=bot_headers,
                timeout=3
            )
            name = r.json().get("username", str(user_id)) if r.status_code == 200 else str(user_id)
        except Exception:
            name = str(user_id)
        user_cache[user_id] = name
        return name

    def fmt_order(order):
        created = order.get("created_at")
        created_str = created.strftime("%d %b %Y, %H:%M") if created else "Unknown"
        return {
            "_id": str(order.get("_id", "")),
            "build_name": order.get("build_name", "Unknown"),
            "buyer_name": resolve_user(order.get("buyer_id")),
            "builder_name": resolve_user(order.get("builder_id")) if order.get("builder_id") else None,
            "ign": order.get("ign", "—"),
            "region": order.get("region", "—"),
            "farm_name": order.get("farm_name", "—"),
            "price": order.get("price", "—"),
            "status": order.get("status", "unknown"),
            "created_at": created_str,
            "payment_status": order.get("payment_status", "confirmed"),
        }

    active_statuses = {"unpaid", "confirmed", "claimed", "payment_pending"}
    active_orders   = [fmt_order(o) for o in all_orders if o.get("status") in active_statuses or o.get("payment_status") == "pending"]
    completed_orders = [fmt_order(o) for o in all_orders if o.get("status") == "completed"]
    cancelled_orders = [fmt_order(o) for o in all_orders if o.get("status") == "cancelled"]

    # Builder stats
    builder_map = {}
    for order in all_orders:
        bid = order.get("builder_id")
        if not bid:
            continue
        if bid not in builder_map:
            builder_map[bid] = {
                "name": resolve_user(bid),
                "orders": [],
                "completed": 0,
                "active": 0,
                "cancelled": 0,
                "first_build_dt": order.get("created_at"),
            }
        status = order.get("status", "")
        builder_map[bid]["orders"].append(fmt_order(order))
        if status == "completed":
            builder_map[bid]["completed"] += 1
        elif status in ("claimed", "confirmed"):
            builder_map[bid]["active"] += 1
        elif status == "cancelled":
            builder_map[bid]["cancelled"] += 1
        dt = order.get("created_at")
        if dt and (builder_map[bid]["first_build_dt"] is None or dt < builder_map[bid]["first_build_dt"]):
            builder_map[bid]["first_build_dt"] = dt

    builder_stats = []
    for data in builder_map.values():
        dt = data["first_build_dt"]
        data["first_build"] = dt.strftime("%d %b %Y") if dt else "Unknown"
        del data["first_build_dt"]
        builder_stats.append(data)
    builder_stats.sort(key=lambda b: (b["completed"], b["active"]), reverse=True)

    return render_template(
        "builds_dashboard.html",
        guild_id=guild_id,
        guild_name=guild_name,
        active_orders=active_orders,
        completed_orders=completed_orders,
        cancelled_orders=cancelled_orders,
        builder_stats=builder_stats,
    )

@app.route("/dashboard/<int:guild_id>/builds/delete", methods=["POST"])
def delete_build_order(guild_id):
    if "access_token" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not bot_in_guild(guild_id):
        return jsonify({"success": False, "error": "Bot is not in this server"}), 403
    if db is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 500
    guilds = session.get("guilds", [])
    if not any(int(g["id"]) == guild_id for g in guilds):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data = request.get_json()
    order_id = data.get("order_id") if data else None
    if not order_id:
        return jsonify({"success": False, "error": "Missing order_id"}), 400
    try:
        result = db["building_orders"].delete_one({
            "_id": ObjectId(order_id),
            "guild_id": guild_id
        })
        if result.deleted_count == 0:
            return jsonify({"success": False, "error": "Order not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"[DELETE_ORDER] {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/test-mongodb")
def test_mongodb_route():
    """Diagnostic endpoint to test MongoDB connection."""
    if db is None:
        return jsonify({"error": "Database not connected"}), 500
    try:
        test_guild = 999999
        test_cmd = "_test_command_"
        result = db["command_perms"].update_one(
            {"guild_id": test_guild, "command_name": test_cmd},
            {"$set": {"roles": ["test_role"], "guild_id": test_guild, "command_name": test_cmd}},
            upsert=True
        )
        doc = db["command_perms"].find_one({"guild_id": test_guild, "command_name": test_cmd})
        db["command_perms"].delete_one({"guild_id": test_guild, "command_name": test_cmd})
        return jsonify({
            "success": True,
            "insert_result": str(result.raw_result),
            "document_found": doc is not None,
            "document": str(doc)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────
# ADD THESE TO app.py before `if __name__ == "__main__":`
# ─────────────────────────────────────────────────────────────────────

MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")


def _current_discord_id():
    """The Discord user ID of whoever is logged into the dashboard right now."""
    return session.get("discord_user", {}).get("id")


@app.route("/mc-login")
def mc_login():
    if "access_token" not in session:
        return redirect("/")
    if not _current_discord_id():
        # Old session predating the discord_user field — force a fresh login
        return redirect("/login")
    return render_template("mc_login.html")


@app.route("/mc-status")
def mc_status():
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.get(f"{MC_BOT_URL}/status/{discord_id}", timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "error": f"MC bot unreachable: {e}"}), 503


@app.route("/mc-start-login", methods=["POST"])
def mc_start_login():
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/start-login/{discord_id}", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-reconnect", methods=["POST"])
def mc_reconnect():
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/reconnect/{discord_id}", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-logout", methods=["POST"])
def mc_logout():
    # Leave server but keep token — bot will reconnect next time
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/logout/{discord_id}", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-full-logout", methods=["POST"])
def mc_full_logout():
    # Disconnect AND wipe saved token (forces re-login next time)
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/full-logout/{discord_id}", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-run-command", methods=["POST"])
def mc_run_command():
    # Run an in-game command as the logged-in user's own linked account
    discord_id = _current_discord_id()
    if not discord_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/run-command/{discord_id}", json=request.get_json(silent=True) or {}, timeout=12)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


# ── ADD THESE ROUTES TO app.py ────────────────────────────────────────────────
# Paste before the `if __name__ == "__main__":` line
# These handle the ticket type + panel builder at /dashboard/<guild_id>/tickets

from bson import ObjectId as _ObjId


@app.route("/dashboard/<int:guild_id>/tickets", methods=["GET", "POST"])
def tickets_dashboard(guild_id):
    if "access_token" not in session:
        return redirect("/")
    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)
    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")
    if db is None:
        return "<h1>Database unavailable</h1>", 500

    bot_headers  = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    roles_res    = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/roles", headers=bot_headers)
    roles        = [r for r in (roles_res.json() if roles_res.ok else []) if r["name"] != "@everyone" and not r["managed"]]
    chans_res    = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=bot_headers)
    text_channels = [c for c in (chans_res.json() if chans_res.ok else []) if c["type"] == 0]

    if request.method == "POST":
        form_type = request.form.get("form_type")

        # ── Create / update ticket type ───────────────────────────────────
        if form_type == "save_ticket_type":
            type_id  = request.form.get("type_id", "").strip()
            name     = request.form.get("name", "").strip()
            emoji    = request.form.get("emoji", "🎫").strip()
            color_hex = request.form.get("color", "#5865f2").lstrip("#")
            category = request.form.get("category", "").strip() or name
            ping_role = request.form.get("ping_role", "").strip()
            allow_roles = [r for r in request.form.getlist("allow_roles") if r]
            button_style = request.form.get("button_style", "primary")
            questions = [q.strip() for q in [
                request.form.get(f"q{i}", "") for i in range(5)
            ] if q.strip()]

            if not name or not questions:
                return redirect(f"/dashboard/{guild_id}/tickets")

            try:
                color = int(color_hex, 16)
            except ValueError:
                color = 0x5865F2

            doc = {
                "guild_id":     guild_id,
                "name":         name,
                "emoji":        emoji,
                "color":        color,
                "category":     category,
                "ping_role":    ping_role,
                "allow_roles":  allow_roles,
                "button_style": button_style,
                "questions":    questions[:5],
            }

            if type_id:
                db["ticket_types"].update_one({"_id": _ObjId(type_id)}, {"$set": doc})
            else:
                db["ticket_types"].insert_one(doc)

        # ── Delete ticket type ────────────────────────────────────────────
        elif form_type == "delete_ticket_type":
            type_id = request.form.get("type_id", "").strip()
            if type_id:
                db["ticket_types"].delete_one({"_id": _ObjId(type_id)})

        # ── Create / update panel ─────────────────────────────────────────
        elif form_type == "save_panel":
            panel_id     = request.form.get("panel_id", "").strip()
            panel_name   = request.form.get("panel_name", "").strip()
            panel_title  = request.form.get("panel_title", "🎫 Open a Ticket").strip()
            panel_desc   = request.form.get("panel_desc", "Click a button below to open a ticket.").strip()
            panel_color_hex = request.form.get("panel_color", "#5865f2").lstrip("#")
            type_ids     = request.form.getlist("panel_type_ids")

            try:
                panel_color = int(panel_color_hex, 16)
            except ValueError:
                panel_color = 0x5865F2

            if not panel_name:
                return redirect(f"/dashboard/{guild_id}/tickets")

            pdoc = {
                "guild_id":        guild_id,
                "name":            panel_name,
                "title":           panel_title,
                "description":     panel_desc,
                "color":           panel_color,
                "ticket_type_ids": type_ids,
            }

            if panel_id:
                db["ticket_panels"].update_one({"_id": _ObjId(panel_id)}, {"$set": pdoc})
            else:
                db["ticket_panels"].insert_one(pdoc)

        # ── Delete panel ──────────────────────────────────────────────────
        elif form_type == "delete_panel":
            panel_id = request.form.get("panel_id", "").strip()
            if panel_id:
                db["ticket_panels"].delete_one({"_id": _ObjId(panel_id)})

        # ── Post panel to a channel ───────────────────────────────────────
        elif form_type == "post_panel":
            panel_id   = request.form.get("panel_id", "").strip()
            channel_id = request.form.get("channel_id", "").strip()

            panel = db["ticket_panels"].find_one({"_id": _ObjId(panel_id)}) if panel_id else None
            if panel and channel_id:
                type_ids = panel.get("ticket_type_ids", [])
                types    = list(db["ticket_types"].find({"_id": {"$in": [_ObjId(t) for t in type_ids]}}))

                # Build components (buttons)
                components = []
                row_items  = []
                for i, tt in enumerate(types[:5]):
                    style_map = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}
                    style_val = style_map.get(tt.get("button_style", "primary"), 1)
                    label     = f"{tt.get('emoji','🎫')} {tt['name']}"[:80]
                    row_items.append({
                        "type":      2,
                        "label":     label,
                        "style":     style_val,
                        "custom_id": f"dyn_ticket_{str(panel['_id'])}_{str(tt['_id'])}",
                    })

                if row_items:
                    components.append({"type": 1, "components": row_items})

                color_int = panel.get("color", 0x5865F2)
                embed_payload = {
                    "title":       panel.get("title", "🎫 Open a Ticket"),
                    "description": panel.get("description", "Click a button below to open a ticket."),
                    "color":       color_int,
                }

                requests.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={**bot_headers, "Content-Type": "application/json"},
                    json={"embeds": [embed_payload], "components": components},
                )

        return redirect(f"/dashboard/{guild_id}/tickets")

    # GET
    ticket_types = list(db["ticket_types"].find({"guild_id": guild_id}))
    panels       = list(db["ticket_panels"].find({"guild_id": guild_id}))

    # Convert ObjectIds to strings for the template
    for t in ticket_types:
        t["_id"] = str(t["_id"])
        t["color_hex"] = f"#{t.get('color', 0x5865F2):06x}"
    for p in panels:
        p["_id"]   = str(p["_id"])
        p["color_hex"] = f"#{p.get('color', 0x5865F2):06x}"
        # Attach type names for display
        assigned_ids = p.get("ticket_type_ids", [])
        p["assigned_types"] = [
            t for t in ticket_types if t["_id"] in assigned_ids
        ]

    guild_name = next((g["name"] for g in session.get("guilds", []) if int(g["id"]) == guild_id), "Server")

    return render_template(
        "tickets_dashboard.html",
        guild_id=guild_id,
        guild_name=guild_name,
        roles=roles,
        channels=text_channels,
        ticket_types=ticket_types,
        panels=panels,
    )

# ── Bot Console: run bot actions on-demand from the dashboard ───────────────

def _discord_api(method, path, reason=None, **kwargs):
    """Thin wrapper around the Discord REST API using the bot token."""
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    if reason:
        headers["X-Audit-Log-Reason"] = reason[:512]
    return requests.request(method, f"https://discord.com/api/v10{path}", headers=headers, timeout=10, **kwargs)


def _discord_err(r):
    """Turn a failed Discord API response into a human-readable message."""
    try:
        body = r.json()
    except Exception:
        return r.text[:300] or f"HTTP {r.status_code}"
    msg = body.get("message", f"HTTP {r.status_code}")
    if r.status_code == 403:
        return f"{msg} — the bot is missing a permission here."
    if r.status_code == 404:
        return f"{msg} — that channel/message may not exist anymore."
    return msg


def _require_guild_access_json(guild_id):
    """For AJAX/JSON routes: verify the session can manage this guild.
    Returns a Flask response to short-circuit with if access fails, else None.
    Logs the mismatch so a stale-session issue is provable from the logs."""
    if "access_token" not in session:
        return jsonify({"ok": False, "error": "Your session has expired. Please log out and log back in."}), 401

    session_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in session_guild_ids:
        logger.warning(
            f"Guild access check failed for guild {guild_id}. "
            f"Session currently has access to: {session_guild_ids}. "
            f"discord_user={session.get('discord_user')}"
        )
        return jsonify({
            "ok": False,
            "error": "Your session no longer shows access to this server (permissions changed, or the "
                     "dashboard was redeployed since you logged in). Please log out and log back in.",
        }), 403
    return None


def _log_console_action(guild_id, action, target_id, detail, ok, error=None):
    if db is None:
        return
    actor = session.get("discord_user", {})
    try:
        db["console_actions"].insert_one({
            "guild_id": guild_id,
            "action": action,
            "target_id": target_id,
            "detail": detail,
            "ok": ok,
            "error": error,
            "actor_id": actor.get("id"),
            "actor_name": actor.get("username"),
            "timestamp": datetime.utcnow(),
        })
    except Exception as e:
        logger.error(f"Failed to log console action: {e}")


@app.route("/dashboard/<int:guild_id>/console", methods=["GET", "POST"])
def bot_console(guild_id):
    """Live control panel: send messages/DMs and moderate members as the bot."""
    if "access_token" not in session:
        return redirect("/")

    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)

    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")

    if not BOT_TOKEN:
        return "<h1>Error: DISCORD_BOT_TOKEN or DISCORD_TOKEN is missing from environment variables!</h1>", 500

    if request.method == "POST":
        form_type = request.form.get("form_type")
        user_id = request.form.get("user_id", "").strip()
        ok, error, detail = False, None, ""

        try:
            if form_type == "send_message":
                channel_id = request.form.get("channel_id")
                content = request.form.get("content", "").strip()
                embed_title = request.form.get("embed_title", "").strip()
                embed_desc = request.form.get("embed_description", "").strip()
                embed_color = request.form.get("embed_color", "#5865f2").lstrip("#")

                payload = {}
                if content:
                    payload["content"] = content
                if embed_title or embed_desc:
                    try:
                        color = int(embed_color, 16)
                    except ValueError:
                        color = 0x5865F2
                    embed = {"color": color}
                    if embed_title:
                        embed["title"] = embed_title
                    if embed_desc:
                        embed["description"] = embed_desc
                    payload["embeds"] = [embed]

                if not channel_id or not payload:
                    error = "Channel and message content or embed are required."
                else:
                    r = _discord_api("POST", f"/channels/{channel_id}/messages", json=payload)
                    ok = r.ok
                    error = None if ok else _discord_err(r)
                    detail = f"channel #{channel_id}: {content[:80] or embed_title}"

            elif form_type == "dm_user":
                content = request.form.get("content", "").strip()
                if not user_id or not content:
                    error = "User ID and message are required."
                else:
                    dm = _discord_api("POST", "/users/@me/channels", json={"recipient_id": user_id})
                    if not dm.ok:
                        ok, error = False, _discord_err(dm)
                    else:
                        dm_channel_id = dm.json()["id"]
                        r = _discord_api("POST", f"/channels/{dm_channel_id}/messages", json={"content": content})
                        ok = r.ok
                        error = None if ok else _discord_err(r)
                    detail = content[:80]

            elif form_type == "kick_member":
                reason = request.form.get("reason", "").strip() or "No reason given"
                if not user_id:
                    error = "User ID is required."
                else:
                    r = _discord_api("DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason)
                    ok = r.ok
                    error = None if ok else _discord_err(r)
                    detail = reason

            elif form_type == "ban_member":
                reason = request.form.get("reason", "").strip() or "No reason given"
                delete_days = request.form.get("delete_days", "0")
                try:
                    delete_seconds = max(0, min(7, int(delete_days))) * 86400
                except ValueError:
                    delete_seconds = 0
                if not user_id:
                    error = "User ID is required."
                else:
                    r = _discord_api(
                        "PUT", f"/guilds/{guild_id}/bans/{user_id}",
                        reason=reason, json={"delete_message_seconds": delete_seconds},
                    )
                    ok = r.ok
                    error = None if ok else _discord_err(r)
                    detail = reason

            elif form_type == "unban_member":
                if not user_id:
                    error = "User ID is required."
                else:
                    r = _discord_api("DELETE", f"/guilds/{guild_id}/bans/{user_id}")
                    ok = r.ok
                    error = None if ok else _discord_err(r)

            elif form_type == "timeout_member":
                reason = request.form.get("reason", "").strip() or "No reason given"
                minutes = request.form.get("minutes", "10")
                try:
                    minutes = max(1, min(40320, int(minutes)))  # Discord max is 28 days
                except ValueError:
                    minutes = 10
                if not user_id:
                    error = "User ID is required."
                else:
                    until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat() + "Z"
                    r = _discord_api(
                        "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                        reason=reason, json={"communication_disabled_until": until},
                    )
                    ok = r.ok
                    error = None if ok else _discord_err(r)
                    detail = f"{minutes}m — {reason}"

            elif form_type == "remove_timeout":
                if not user_id:
                    error = "User ID is required."
                else:
                    r = _discord_api(
                        "PATCH", f"/guilds/{guild_id}/members/{user_id}",
                        json={"communication_disabled_until": None},
                    )
                    ok = r.ok
                    error = None if ok else _discord_err(r)

            elif form_type in ("add_role", "remove_role"):
                role_id = request.form.get("role_id")
                if not user_id or not role_id:
                    error = "User ID and role are required."
                else:
                    method = "PUT" if form_type == "add_role" else "DELETE"
                    r = _discord_api(method, f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
                    ok = r.ok
                    error = None if ok else _discord_err(r)
                    detail = f"role {role_id}"

            else:
                error = "Unknown action."

        except Exception as e:
            logger.error(f"Console action '{form_type}' failed: {e}")
            ok, error = False, str(e)[:300]

        _log_console_action(guild_id, form_type, user_id, detail, ok, error)

        status = "ok" if ok else "error"
        msg = error or "Done."
        return redirect(f"/dashboard/{guild_id}/console?status={status}&msg={msg}")

    # ── GET: render the console ──────────────────────────────────────────
    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}

    roles_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/roles", headers=bot_headers)
    roles = [r for r in (roles_res.json() if roles_res.ok else []) if r["name"] != "@everyone" and not r["managed"]]
    roles.sort(key=lambda r: r["position"], reverse=True)

    chans_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=bot_headers)
    text_channels = [c for c in (chans_res.json() if chans_res.ok else []) if c["type"] == 0]

    recent_actions = []
    if db is not None:
        recent_actions = list(
            db["console_actions"].find({"guild_id": guild_id}).sort("timestamp", -1).limit(20)
        )

    guild_name = next((g["name"] for g in session.get("guilds", []) if int(g["id"]) == guild_id), "Server")

    return render_template(
        "console.html",
        guild_id=guild_id,
        guild_name=guild_name,
        roles=roles,
        channels=text_channels,
        recent_actions=recent_actions,
        status=request.args.get("status"),
        status_msg=request.args.get("msg"),
    )


@app.route("/dashboard/<int:guild_id>/console/lookup")
def console_lookup_user(guild_id):
    """AJAX helper: search guild members by name so staff can grab a user ID."""
    if "access_token" not in session:
        return jsonify([]), 401

    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        return jsonify([]), 403

    query = request.args.get("query", "").strip()
    if not query or not BOT_TOKEN:
        return jsonify([])

    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    r = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/members/search",
        headers=bot_headers, params={"query": query, "limit": 8}, timeout=5,
    )
    if not r.ok:
        return jsonify([])

    results = []
    for m in r.json():
        user = m.get("user", {})
        results.append({
            "id": user.get("id"),
            "username": user.get("username"),
            "display_name": m.get("nick") or user.get("global_name") or user.get("username"),
            "avatar": user.get("avatar"),
        })
    return jsonify(results)


def _avatar_url(user):
    uid = user.get("id")
    ah = user.get("avatar")
    if ah:
        ext = "gif" if ah.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{uid}/{ah}.{ext}"
    try:
        default_idx = (int(uid) >> 22) % 6
    except (TypeError, ValueError):
        default_idx = 0
    return f"https://cdn.discordapp.com/embed/avatars/{default_idx}.png"


def _serialize_message(m):
    author = m.get("author", {})
    ref = m.get("referenced_message")
    return {
        "id": m.get("id"),
        "channel_id": m.get("channel_id"),
        "content": m.get("content", ""),
        "timestamp": m.get("timestamp"),
        "edited_timestamp": m.get("edited_timestamp"),
        "author": {
            "id": author.get("id"),
            "username": author.get("username", "Unknown"),
            "bot": author.get("bot", False),
            "avatar_url": _avatar_url(author),
        },
        "attachments": [
            {"url": a.get("url"), "filename": a.get("filename")} for a in m.get("attachments", [])
        ],
        "embeds": [
            {"title": e.get("title"), "description": e.get("description")} for e in m.get("embeds", [])
        ],
        "reply_to": (
            {
                "id": ref.get("id"),
                "author": (ref.get("author") or {}).get("username", "Unknown"),
                "content": (ref.get("content") or "")[:120],
            }
            if ref else None
        ),
        "is_bot_self": author.get("id") == _bot_user_id(),
    }


_bot_user_id_cache = {"id": None}


def _bot_user_id():
    if _bot_user_id_cache["id"] is None and BOT_TOKEN:
        try:
            r = _discord_api("GET", "/users/@me")
            if r.ok:
                _bot_user_id_cache["id"] = r.json().get("id")
        except Exception:
            pass
    return _bot_user_id_cache["id"]


@app.route("/dashboard/<int:guild_id>/console/chat")
def bot_chat(guild_id):
    """Live chat view: read a channel's recent messages and reply/send/forward as the bot."""
    if "access_token" not in session:
        return redirect("/")

    user_guild_ids = [int(g["id"]) for g in session.get("guilds", [])]
    if guild_id not in user_guild_ids:
        abort(403)

    if not bot_in_guild(guild_id):
        return redirect(f"/dashboard?bot_missing={guild_id}")

    if not BOT_TOKEN:
        return "<h1>Error: DISCORD_BOT_TOKEN or DISCORD_TOKEN is missing from environment variables!</h1>", 500

    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    chans_res = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=bot_headers)
    text_channels = [c for c in (chans_res.json() if chans_res.ok else []) if c["type"] == 0]
    text_channels.sort(key=lambda c: c.get("position", 0))

    guild_name = next((g["name"] for g in session.get("guilds", []) if int(g["id"]) == guild_id), "Server")

    return render_template(
        "chat.html",
        guild_id=guild_id,
        guild_name=guild_name,
        channels=text_channels,
    )


@app.route("/dashboard/<int:guild_id>/console/chat/messages")
def chat_fetch_messages(guild_id):
    """AJAX: fetch a channel's messages. Pass ?after=<id> to poll for new ones only."""
    access_error = _require_guild_access_json(guild_id)
    if access_error:
        return access_error

    channel_id = request.args.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id required"}), 400

    params = {"limit": 50}
    after = request.args.get("after")
    before = request.args.get("before")
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    r = _discord_api("GET", f"/channels/{channel_id}/messages", params=params)
    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = {}
        discord_msg = body.get("message", r.text[:200])
        discord_code = body.get("code")
        logger.warning(
            f"Chat fetch failed for channel {channel_id} in guild {guild_id}: "
            f"{r.status_code} {discord_msg} (code {discord_code})"
        )
        if r.status_code == 403:
            friendly = "The bot can't view this channel — check its role has View Channel / Read Message History here."
        elif r.status_code == 404:
            friendly = "Channel not found — it may have been deleted."
        else:
            friendly = discord_msg
        return jsonify({"error": friendly, "discord_code": discord_code}), r.status_code

    messages = [_serialize_message(m) for m in r.json()]
    messages.sort(key=lambda m: m["id"])  # ascending, oldest first
    return jsonify({"messages": messages})


@app.route("/dashboard/<int:guild_id>/console/chat/send", methods=["POST"])
def chat_send_message(guild_id):
    access_error = _require_guild_access_json(guild_id)
    if access_error:
        return access_error

    data = request.get_json(silent=True) or {}
    channel_id = data.get("channel_id")
    content = (data.get("content") or "").strip()
    reply_to = data.get("reply_to")

    if not channel_id or not content:
        return jsonify({"ok": False, "error": "channel_id and content are required"}), 400
    if len(content) > 2000:
        content = content[:2000]

    payload = {"content": content}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}

    r = _discord_api("POST", f"/channels/{channel_id}/messages", json=payload)
    ok = r.ok
    _log_console_action(
        guild_id, "chat_reply" if reply_to else "chat_send", channel_id,
        content[:80], ok, None if ok else _discord_err(r),
    )
    if not ok:
        return jsonify({"ok": False, "error": _discord_err(r)}), r.status_code
    return jsonify({"ok": True, "message": _serialize_message(r.json())})


@app.route("/dashboard/<int:guild_id>/console/chat/forward", methods=["POST"])
def chat_forward_message(guild_id):
    """Copies a message's content (and attachment links) into another channel,
    clearly labelled as forwarded. (Uses a labelled copy rather than Discord's
    native forward snapshot, so it works reliably across API versions.)"""
    if "access_token" not in session:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    access_error = _require_guild_access_json(guild_id)
    if access_error:
        return access_error

    data = request.get_json(silent=True) or {}
    source_channel_id = data.get("source_channel_id")
    dest_channel_id = data.get("dest_channel_id")
    author_name = data.get("author_name", "Unknown")
    content = (data.get("content") or "").strip()
    attachments = data.get("attachment_urls") or []

    if not source_channel_id or not dest_channel_id:
        return jsonify({"ok": False, "error": "source and destination channels are required"}), 400

    quoted = "\n".join(f"> {line}" for line in (content or "*[no text]*").splitlines()) or "> *[no text]*"
    body = f"↪️ **Forwarded from <#{source_channel_id}>**, originally by **{author_name}**:\n{quoted}"
    if attachments:
        body += "\n" + "\n".join(attachments)
    body = body[:2000]

    r = _discord_api("POST", f"/channels/{dest_channel_id}/messages", json={"content": body})
    ok = r.ok
    _log_console_action(
        guild_id, "chat_forward", dest_channel_id,
        f"from #{source_channel_id}", ok, None if ok else _discord_err(r),
    )
    if not ok:
        return jsonify({"ok": False, "error": _discord_err(r)}), r.status_code
    return jsonify({"ok": True, "message": _serialize_message(r.json())})


@app.route("/dashboard/<int:guild_id>/console/chat/delete", methods=["POST"])
def chat_delete_message(guild_id):
    access_error = _require_guild_access_json(guild_id)
    if access_error:
        return access_error

    data = request.get_json(silent=True) or {}
    channel_id = data.get("channel_id")
    message_id = data.get("message_id")
    if not channel_id or not message_id:
        return jsonify({"ok": False, "error": "channel_id and message_id are required"}), 400

    r = _discord_api("DELETE", f"/channels/{channel_id}/messages/{message_id}")
    ok = r.ok
    _log_console_action(guild_id, "chat_delete", channel_id, message_id, ok, None if ok else _discord_err(r))
    if not ok:
        return jsonify({"ok": False, "error": _discord_err(r)}), r.status_code
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)