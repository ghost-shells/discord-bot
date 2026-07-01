# app.py – full file with payment log channel added

import os
from flask import Flask, render_template, redirect, request, session, Response, jsonify
import requests
from dotenv import load_dotenv
from db import get_bot_token, get_db, test_mongodb
from bson.objectid import ObjectId
from flask import abort
from datetime import datetime
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


@app.route("/")
def index():
    return render_template("index.html", client_id=CLIENT_ID, redirect_uri=REDIRECT_URI)


@app.route("/login")
def login():
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

    session["access_token"] = tokens["access_token"]

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
    return render_template("dashboard.html", guilds=session.get("guilds", []))


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


@app.route("/mc-login")
def mc_login():
    if "access_token" not in session:
        return redirect("/")
    return render_template("mc_login.html")


@app.route("/mc-status")
def mc_status():
    if "access_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.get(f"{MC_BOT_URL}/status", timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "error": f"MC bot unreachable: {e}"}), 503


@app.route("/mc-start-login", methods=["POST"])
def mc_start_login():
    if "access_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/start-login", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-reconnect", methods=["POST"])
def mc_reconnect():
    if "access_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/reconnect", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-logout", methods=["POST"])
def mc_logout():
    # Leave server but keep token — bot will reconnect next time
    if "access_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/logout", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/mc-full-logout", methods=["POST"])
def mc_full_logout():
    # Disconnect AND wipe saved token (forces re-login next time)
    if "access_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        r = requests.post(f"{MC_BOT_URL}/full-logout", timeout=10)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)