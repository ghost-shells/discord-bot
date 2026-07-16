# cogs/tickets.py
# Fully dynamic ticket system — all types and panels stored in MongoDB.
# Replaces: tickets_base.py, tickets_bedrock.py, tickets_spawner.py, tickets_support.py
# Building tickets are NOT touched — still handled by building.py

import os
import re
import html
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from bson import ObjectId
from jinja2 import Template

from cogs.config import admin_only, get_guild_config, TICKET_PREFIXES

# ── Constants ─────────────────────────────────────────────────────────────────

BUTTON_STYLES = {
    "primary":   discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success":   discord.ButtonStyle.success,
    "danger":    discord.ButtonStyle.danger,
}

# ── MongoDB helpers ───────────────────────────────────────────────────────────

def get_ticket_types(db, guild_id: int) -> list[dict]:
    return list(db["ticket_types"].find({"guild_id": guild_id}))

def get_ticket_type(db, type_id: str) -> dict | None:
    try:
        return db["ticket_types"].find_one({"_id": ObjectId(type_id)})
    except Exception:
        return None

def get_panel(db, panel_id: str) -> dict | None:
    try:
        return db["ticket_panels"].find_one({"_id": ObjectId(panel_id)})
    except Exception:
        return None

def get_panels(db, guild_id: int) -> list[dict]:
    return list(db["ticket_panels"].find({"guild_id": guild_id}))

# ── Channel helpers ───────────────────────────────────────────────────────────

def get_creator_name(channel: discord.TextChannel) -> str:
    if channel.topic and "Ticket by " in channel.topic:
        try:
            return channel.topic.split("Ticket by ")[1].split(" |")[0].strip().lower()
        except IndexError:
            pass
    name = channel.name
    for prefix in TICKET_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.lower()

def is_ticket_channel(channel: discord.TextChannel) -> bool:
    return any(channel.name.startswith(p) for p in TICKET_PREFIXES)

def has_ticket_topic(channel: discord.TextChannel) -> bool:
    """Rename-proof ticket check. Every ticket type (regular tickets, application
    tickets, and giveaway claim tickets) stamps 'Ticket by X | Category' or
    'Buyer: X | ...' into the channel topic when it's created, and that topic
    survives channel renames — unlike the name-prefix check in is_ticket_channel."""
    topic = channel.topic or ""
    return "Ticket by " in topic or "Buyer: " in topic

async def check_existing_ticket(interaction: discord.Interaction) -> bool:
    uname = interaction.user.name.lower()
    for ch in interaction.guild.text_channels:
        if is_ticket_channel(ch) and ch.name.endswith(f"-{uname}"):
            await interaction.response.send_message(
                f"❌ You already have an open ticket: {ch.mention}", ephemeral=True
            )
            return True
    return False

# ── Ticket channel creator ────────────────────────────────────────────────────

async def create_ticket_channel(
    interaction: discord.Interaction,
    ticket_type: dict,
    answers: dict,
):
    uname   = interaction.user.name.lower()
    db      = interaction.client.db
    guild   = interaction.guild

    # Resolve roles
    ping_role_name  = ticket_type.get("ping_role", "")
    allow_role_names = ticket_type.get("allow_roles", [])
    color   = ticket_type.get("color", 0x5865f2)
    emoji   = ticket_type.get("emoji", "🎫")
    cat_name = ticket_type.get("category", "Tickets")
    name    = ticket_type.get("name", "Ticket")

    # Get or create Discord category
    dc_cat = discord.utils.get(guild.categories, name=cat_name)
    if not dc_cat:
        dc_cat = await guild.create_category(cat_name)
        await dc_cat.set_permissions(guild.default_role, read_messages=False)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(
            read_messages=True, send_messages=True,
            read_message_history=True, attach_files=True
        ),
    }
    for role_name in allow_role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                read_message_history=True, attach_files=True
            )

    channel = await guild.create_text_channel(
        name=f"ticket-{uname}",
        category=dc_cat,
        overwrites=overwrites,
        topic=f"Ticket by {interaction.user.name} | {name}",
    )

    embed = discord.Embed(
        title=f"{emoji} {name} Ticket",
        description=f"### Welcome {interaction.user.mention}!\n\n━━━━━━━━━━━━━━━━━━",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    for question, answer in answers.items():
        embed.add_field(name=question, value=answer or "*No answer provided*", inline=False)
    embed.add_field(name="Created By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Category",   value=name, inline=True)
    embed.set_footer(text=f"Channel ID: {channel.id}")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    view = TicketView()
    await channel.send(embed=embed, view=view)

    # Ping role
    if ping_role_name:
        ping_role = discord.utils.get(guild.roles, name=ping_role_name)
        if ping_role:
            await channel.send(
                f"{ping_role.mention}\nNew **{name}** ticket from {interaction.user.mention}!"
            )

    await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)

# ── Dynamic Modal ─────────────────────────────────────────────────────────────

def make_modal(ticket_type: dict):
    """Build a discord.ui.Modal dynamically from a ticket_type document."""
    questions = ticket_type.get("questions", ["What do you need help with?"])
    name      = ticket_type.get("name", "Ticket")
    emoji     = ticket_type.get("emoji", "🎫")
    type_id   = str(ticket_type["_id"])

    # Discord modals support max 5 fields
    questions = questions[:5]

    attrs = {}
    for i, q in enumerate(questions):
        field = discord.ui.TextInput(
            label=q[:45],  # Discord label max 45 chars
            style=discord.TextStyle.paragraph if i == len(questions) - 1 else discord.TextStyle.short,
            required=(i == 0),  # Only first question required
            custom_id=f"q{i}",
        )
        attrs[f"q{i}"] = field

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = interaction.client.db
        tt = get_ticket_type(db, self._type_id)
        if not tt:
            return await interaction.followup.send("❌ Ticket type no longer exists.", ephemeral=True)
        qs = tt.get("questions", [])[:5]
        answers = {}
        for i, q in enumerate(qs):
            field = getattr(self, f"q{i}", None)
            answers[q] = field.value if field else ""
        await create_ticket_channel(interaction, tt, answers)

    attrs["on_submit"] = on_submit
    attrs["_type_id"]  = type_id

    ModalClass = type(
        f"TicketModal_{type_id}",
        (discord.ui.Modal,),
        {**attrs, "__discord_ui_modal__": True},
    )
    # Set the title via the class (Discord reads it from the class attribute)
    modal = ModalClass(title=f"{emoji} {name[:40]} Ticket")
    modal._type_id = type_id
    return modal

# ── Dynamic Panel View ────────────────────────────────────────────────────────

def make_panel_view(panel: dict, ticket_types: list[dict]) -> discord.ui.View:
    """Build a persistent View from a panel document + its ticket types."""
    panel_id = str(panel["_id"])
    type_ids = panel.get("ticket_type_ids", [])

    # Filter to only types assigned to this panel, preserving order
    assigned = [tt for tid in type_ids for tt in ticket_types if str(tt["_id"]) == tid]

    view = discord.ui.View(timeout=None)

    for i, tt in enumerate(assigned[:5]):  # Discord max 5 buttons per row group
        type_id   = str(tt["_id"])
        label     = f"{tt.get('emoji','🎫')} {tt['name']}"[:80]
        style_key = tt.get("button_style", "primary")
        style     = BUTTON_STYLES.get(style_key, discord.ButtonStyle.primary)
        custom_id = f"dyn_ticket_{panel_id}_{type_id}"

        async def callback(interaction: discord.Interaction, _type_id=type_id):
            if await check_existing_ticket(interaction):
                return
            db = interaction.client.db
            tt2 = get_ticket_type(db, _type_id)
            if not tt2:
                return await interaction.response.send_message(
                    "❌ This ticket type no longer exists.", ephemeral=True
                )
            modal = make_modal(tt2)
            await interaction.response.send_modal(modal)

        btn = discord.ui.Button(label=label, style=style, custom_id=custom_id, row=i // 3)
        btn.callback = callback
        view.add_item(btn)

    return view

# ── Ticket View (inside ticket channel) ──────────────────────────────────────

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Request Close", style=discord.ButtonStyle.grey, custom_id="dyn_req_close_v1")
    async def request_close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg         = get_guild_config(interaction.client.db, interaction.guild.id)
        creator_name = get_creator_name(interaction.channel)
        uname       = interaction.user.name.lower()
        disp        = interaction.user.display_name.lower()
        is_creator  = (
            uname == creator_name or disp == creator_name
            or interaction.channel.name.endswith(f"-{uname}")
            or interaction.channel.name.endswith(f"-{disp}")
        )
        staff_role  = discord.utils.get(interaction.guild.roles, name=cfg["STAFF_ROLE"])
        is_staff    = interaction.user.guild_permissions.administrator or (
            staff_role and staff_role in interaction.user.roles
        )
        if not is_creator and not is_staff:
            return await interaction.response.send_message(
                "❌ Only the ticket creator or staff can request a close.", ephemeral=True
            )
        mention = staff_role.mention if staff_role else f"@{cfg['STAFF_ROLE']}"
        await interaction.response.send_message(
            f"{mention}\n**{interaction.user.mention}** has requested to close this ticket."
        )

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="dyn_close_v1")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg        = get_guild_config(interaction.client.db, interaction.guild.id)
        staff_role = discord.utils.get(interaction.guild.roles, name=cfg["STAFF_ROLE"])
        is_staff   = interaction.user.guild_permissions.administrator or (
            staff_role and staff_role in interaction.user.roles
        )
        if not is_staff:
            return await interaction.response.send_message("❌ Staff only!", ephemeral=True)
        await interaction.response.send_message(
            "🔒 Closing ticket and generating transcript...", ephemeral=True
        )
        await _close_ticket(interaction.channel, interaction.user, interaction.client.db)

# ── Transcript & Close ────────────────────────────────────────────────────────

async def _close_ticket(channel: discord.TextChannel, closed_by: discord.Member, db):
    guild = channel.guild

    messages = []
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            messages.append({
                "id":          msg.id,
                "author":      str(msg.author),
                "author_id":   msg.author.id,
                "timestamp":   msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "content":     msg.content or "",
                "attachments": [{"filename": a.filename, "url": a.url} for a in msg.attachments],
                "is_bot":      msg.author.bot,
                "is_system":   False,
                "avatar_url":  str(msg.author.display_avatar.url) if msg.author.avatar else None,
            })
    except Exception as e:
        print(f"[tickets] Error fetching messages: {e}")

    creator_name = "Unknown"
    category     = "Unknown"
    if channel.topic:
        if "Ticket by " in channel.topic:
            creator_name = channel.topic.split("Ticket by ")[1].split(" |")[0].strip()
            if "|" in channel.topic:
                category = channel.topic.split("|")[1].strip()
        elif "Buyer: " in channel.topic:
            creator_name = channel.topic.split("Buyer: ")[1].split(" |")[0].strip()
            if "Build: " in channel.topic:
                category = "Build: " + channel.topic.split("Build: ")[1].split(" |")[0].strip()

    creator_member = None
    for m in messages:
        if not m["is_bot"] and str(m["author"]).lower().startswith(creator_name.lower()):
            creator_member = guild.get_member(m["author_id"])
            if creator_member:
                break

    html_content = _generate_html(channel, messages, closed_by, creator_name, category)

    transcript_doc = {
        "_id":          ObjectId(),
        "guild_id":     guild.id,
        "guild_name":   guild.name,
        "channel_id":   channel.id,
        "channel_name": channel.name,
        "creator_name": creator_name,
        "category":     category,
        "closed_by":    str(closed_by),
        "closed_by_id": closed_by.id,
        "created_at":   channel.created_at,
        "closed_at":    datetime.now(timezone.utc),
        "message_count": len(messages),
        "html_content": html_content,
        "participants": list(set(m["author_id"] for m in messages if not m["is_bot"])),
    }
    try:
        db["transcripts"].insert_one(transcript_doc)
    except Exception as e:
        print(f"[tickets] Failed to save transcript: {e}")

    dashboard_url = os.getenv("DASHBOARD_URL", "https://your-domain.com")
    cfg           = get_guild_config(db, guild.id)

    tc_id = cfg.get("TRANSCRIPT_CHANNEL_ID")
    if tc_id:
        tc = guild.get_channel(tc_id)
        if tc:
            em = discord.Embed(
                title=f"📑 Ticket Closed: {channel.name}",
                description=(
                    f"**Category:** {category}\n**Creator:** {creator_name}\n"
                    f"**Closed By:** {closed_by.mention}\n**Messages:** {len(messages)}"
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            em.add_field(
                name="View Transcript",
                value=f"[Click here]({dashboard_url}/transcripts/{transcript_doc['_id']})",
                inline=False,
            )
            try:
                await tc.send(embed=em)
            except Exception as e:
                print(f"[tickets] Error sending to transcript channel: {e}")

    if creator_member:
        try:
            dm = discord.Embed(
                title=f"📑 Ticket Closed: {channel.name}",
                description=f"Your ticket in **{guild.name}** has been closed by {closed_by.mention}.",
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            dm.add_field(
                name="View Full Transcript",
                value=f"{dashboard_url}/transcripts/{transcript_doc['_id']}",
                inline=False,
            )
            await creator_member.send(embed=dm)
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[tickets] Error sending DM: {e}")

    try:
        await channel.delete()
    except Exception as e:
        print(f"[tickets] Error deleting channel: {e}")


def _generate_html(channel, messages, closed_by, creator_name, category) -> str:
    processed = []
    for msg in messages:
        content = html.escape(msg.get("content", ""))
        content = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2" target="_blank">\1</a>', content)
        content = re.sub(r'```(.*?)```', r'<pre><code>\1</code></pre>', content, flags=re.DOTALL)
        content = re.sub(r'`(.*?)`', r'<code>\1</code>', content)
        content = content.replace("\n", "<br>")
        processed.append({
            "author":      html.escape(msg.get("author", "Unknown")),
            "timestamp":   msg.get("timestamp", ""),
            "content":     content,
            "attachments": msg.get("attachments", []),
            "is_bot":      msg.get("is_bot", False),
            "is_system":   msg.get("is_system", False),
            "avatar_url":  msg.get("avatar_url", ""),
        })

    TMPL = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Transcript - {{ channel_name }}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#1e1f22;color:#dbdee1;padding:20px}
.container{max-width:1000px;margin:0 auto;background:#2b2d31;border-radius:12px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.3)}
.header{background:#1e1f22;padding:30px;border-bottom:1px solid #3f4147;text-align:center}
.header h1{font-size:28px;color:#5865F2;margin-bottom:10px}
.ticket-id{background:#313338;display:inline-block;padding:6px 12px;border-radius:6px;font-family:monospace;font-size:14px;margin-top:10px}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;padding:20px 30px;background:#1e1f22;border-bottom:1px solid #3f4147}
.info-label{font-size:11px;text-transform:uppercase;color:#949ba4;letter-spacing:.5px;margin-bottom:5px}
.info-value{font-size:14px;font-weight:600}
.messages{padding:20px 30px}
.message{display:flex;gap:16px;padding:16px;margin-bottom:8px;border-radius:8px}
.message:hover{background:#313338}
.avatar{width:40px;height:40px;border-radius:50%;background:#5865F2;display:flex;align-items:center;justify-content:center;font-weight:bold;flex-shrink:0}
.avatar img{width:100%;height:100%;border-radius:50%;object-fit:cover}
.message-content{flex:1}
.message-header{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.author-name{font-weight:600}
.timestamp{font-size:11px;color:#949ba4}
.message-text{font-size:15px;line-height:1.4;word-wrap:break-word}
.message-text a{color:#5865F2;text-decoration:none}
.attachment{background:#1e1f22;padding:8px 12px;border-radius:6px;margin-top:8px;display:inline-block;font-size:13px}
.system-message{background:#313338;opacity:.8}
.system-message .message-text{font-style:italic;color:#949ba4}
.footer{background:#1e1f22;padding:20px;text-align:center;border-top:1px solid #3f4147;font-size:12px;color:#949ba4}
pre{background:#1e1f22;padding:12px;border-radius:6px;overflow-x:auto;font-size:13px;margin-top:8px}
code{font-family:monospace}
</style></head><body>
<div class="container">
<div class="header"><h1>📑 Ticket Transcript</h1><div class="ticket-id">{{ channel_name }}</div></div>
<div class="info-grid">
<div><div class="info-label">Created By</div><div class="info-value">{{ creator_name }}</div></div>
<div><div class="info-label">Category</div><div class="info-value">{{ category }}</div></div>
<div><div class="info-label">Created At</div><div class="info-value">{{ created_at }}</div></div>
<div><div class="info-label">Closed At</div><div class="info-value">{{ closed_at }}</div></div>
<div><div class="info-label">Closed By</div><div class="info-value">{{ closed_by }}</div></div>
<div><div class="info-label">Messages</div><div class="info-value">{{ message_count }}</div></div>
</div>
<div class="messages">
{% for m in messages %}
<div class="message{% if m.is_system %} system-message{% endif %}">
<div class="avatar">{% if m.avatar_url %}<img src="{{ m.avatar_url }}" alt="{{ m.author }}">{% else %}{{ m.author[:1] }}{% endif %}</div>
<div class="message-content">
<div class="message-header"><span class="author-name">{{ m.author }}</span><span class="timestamp">{{ m.timestamp }}</span>{% if m.is_bot %}<span class="timestamp">🤖</span>{% endif %}</div>
<div class="message-text">{{ m.content | safe }}{% if m.attachments %}<div class="attachment">📎 {% for a in m.attachments %}<a href="{{ a.url }}" target="_blank">{{ a.filename }}</a>{% if not loop.last %}, {% endif %}{% endfor %}</div>{% endif %}</div>
</div></div>
{% endfor %}
</div>
<div class="footer">Generated {{ generated_at }} • Closed by {{ closed_by }}</div>
</div></body></html>"""

    return Template(TMPL).render(
        channel_name=html.escape(channel.name),
        creator_name=html.escape(creator_name),
        category=html.escape(category),
        created_at=channel.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        closed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        closed_by=html.escape(str(closed_by)),
        message_count=len(processed),
        messages=processed,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

# ── Cog ───────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Always re-register the static TicketView for close buttons
        bot.add_view(TicketView())
        # Re-register all dynamic panel views from MongoDB
        bot.loop.create_task(self._register_panel_views())

    async def _register_panel_views(self):
        await self.bot.wait_until_ready()
        db = self.bot.db
        if db is None:
            return
        for guild in self.bot.guilds:
            panels = get_panels(db, guild.id)
            types  = get_ticket_types(db, guild.id)
            for panel in panels:
                view = make_panel_view(panel, types)
                self.bot.add_view(view)
        print(f"[tickets] ✅ Registered panel views")

    # /ticketpanel — post a saved panel to current channel
    @app_commands.command(name="ticketpanel", description="Post a saved ticket panel in this channel")
    @app_commands.describe(panel_name="Name of the panel to post")
    @admin_only()
    async def ticketpanel(self, interaction: discord.Interaction, panel_name: str):
        db     = interaction.client.db
        panels = get_panels(db, interaction.guild.id)
        panel  = next((p for p in panels if p["name"].lower() == panel_name.lower()), None)

        if not panel:
            names = ", ".join(f"`{p['name']}`" for p in panels) or "none"
            return await interaction.response.send_message(
                f"❌ No panel named `{panel_name}`. Available: {names}", ephemeral=True
            )

        types = get_ticket_types(db, interaction.guild.id)
        view  = make_panel_view(panel, types)

        embed = discord.Embed(
            title=panel.get("title", "🎫 Open a Ticket"),
            description=panel.get("description", "Click a button below to open a ticket."),
            color=panel.get("color", 0x5865F2),
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        await interaction.response.send_message(embed=embed, view=view)

    @ticketpanel.autocomplete("panel_name")
    async def panel_autocomplete(self, interaction: discord.Interaction, current: str):
        panels = get_panels(interaction.client.db, interaction.guild.id)
        return [
            app_commands.Choice(name=p["name"], value=p["name"])
            for p in panels if current.lower() in p["name"].lower()
        ][:25]

    # /close — close current ticket
    @app_commands.command(name="close", description="Close the current ticket")
    async def close(self, interaction: discord.Interaction):
        if not has_ticket_topic(interaction.channel):
            return await interaction.response.send_message(
                "❌ This can only be used in ticket channels.", ephemeral=True
            )
        cfg        = get_guild_config(interaction.client.db, interaction.guild.id)
        staff_role = discord.utils.get(interaction.guild.roles, name=cfg["STAFF_ROLE"])
        creator    = get_creator_name(interaction.channel)
        uname      = interaction.user.name.lower()

        is_staff   = interaction.user.guild_permissions.administrator or (
            staff_role and staff_role in interaction.user.roles
        )
        is_creator = (
            uname == creator
            or interaction.channel.name.endswith(f"-{uname}")
        )

        if not is_staff and not is_creator:
            return await interaction.response.send_message(
                "❌ You don't have permission to close this ticket.", ephemeral=True
            )

        await interaction.response.send_message(
            "🔒 Closing ticket and generating transcript...", ephemeral=True
        )
        await _close_ticket(interaction.channel, interaction.user, interaction.client.db)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))