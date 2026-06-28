import discord
from discord.ext import commands
from datetime import datetime, timezone
from cogs.config import get_guild_config

# ---------------------------------------------------------------------------
# Application Views
# ---------------------------------------------------------------------------

class ApplyButton(discord.ui.Button):
    def __init__(self, app_id: str, app_name: str):
        super().__init__(
            label=f"Apply for {app_name}",
            style=discord.ButtonStyle.primary,
            custom_id=f"apply_{app_id}",
            emoji="📝"
        )
        self.app_id = app_id
        self.app_name = app_name

    async def callback(self, interaction: discord.Interaction):
        app_config = interaction.client.db["applications_config"].find_one({"guild_id": interaction.guild.id, "app_id": self.app_id})
        
        if not app_config or not app_config.get("is_open", False):
            return await interaction.response.send_message("❌ This application is closed right now.", ephemeral=True)

        # Check if user already has an active session
        existing_session = interaction.client.db["application_sessions"].find_one({"user_id": interaction.user.id})
        if existing_session:
            return await interaction.response.send_message("❌ You already have an active application in progress. Check your DMs!", ephemeral=True)

        try:
            dm_channel = await interaction.user.create_dm()
            questions = app_config.get("questions", [])
            if not questions:
                return await interaction.response.send_message("❌ This application has no questions set up.", ephemeral=True)

            # 1. Send the Green "Application Started" Embed
            start_embed = discord.Embed(
                title="✅ Application Started",
                description=f"You are now applying for **{app_config['app_name']}**.\n\nType your answer in the chat, and I will ask the next question.\nYou can type `cancel` at any time to abort.",
                color=discord.Color.green()
            )
            await dm_channel.send(embed=start_embed)
            
            # 2. Send the Blue "Question 1" Embed
            total_q = len(questions)
            q_embed = discord.Embed(
                title=f"{app_config['app_name']}",
                description=f"**1/{total_q}. {questions[0]}**\n\nType your answer below.",
                color=discord.Color.blue()
            )
            await dm_channel.send(embed=q_embed)
            
            # Save session to MongoDB
            interaction.client.db["application_sessions"].insert_one({
                "user_id": interaction.user.id,
                "guild_id": interaction.guild.id,
                "app_id": self.app_id,
                "current_q": 0,
                "answers": []
            })
            
            await interaction.response.send_message("✅ Check your DMs to start the application!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I couldn't DM you! Please enable DMs from server members in your privacy settings.", ephemeral=True)

class ApplyPanelView(discord.ui.View):
    def __init__(self, app_id: str, app_name: str):
        super().__init__(timeout=None)
        self.add_item(ApplyButton(app_id, app_name))

class ApplicationActionView(discord.ui.View):
    def __init__(self, app_id: str, applicant_id: int, app_config: dict):
        super().__init__(timeout=None)
        self.app_id = app_id
        self.applicant_id = applicant_id
        self.app_config = app_config

        accept_btn = discord.ui.Button(
            label="✅ Accept",
            style=discord.ButtonStyle.green,
            custom_id=f"app_accept_{app_id}_{applicant_id}",
        )
        deny_btn = discord.ui.Button(
            label="❌ Deny",
            style=discord.ButtonStyle.red,
            custom_id=f"app_deny_{app_id}_{applicant_id}",
        )
        ticket_btn = discord.ui.Button(
            label="🎫 Open Ticket",
            style=discord.ButtonStyle.grey,
            custom_id=f"app_ticket_{app_id}_{applicant_id}",
        )
        accept_btn.callback = self.accept_btn
        deny_btn.callback = self.deny_btn
        ticket_btn.callback = self.ticket_btn
        self.add_item(accept_btn)
        self.add_item(deny_btn)
        self.add_item(ticket_btn)

    async def accept_btn(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        accepted_ch = interaction.client.get_channel(self.app_config.get("accepted_channel_id"))
        if accepted_ch:
            new_embed = interaction.message.embeds[0]
            new_embed.color = discord.Color.green()
            new_embed.title = f"✅ Accepted {self.app_config.get('app_name')} Application"
            new_embed.add_field(name="Accepted By", value=interaction.user.mention, inline=False)
            await accepted_ch.send(embed=new_embed)

        try:
            user = await interaction.client.fetch_user(self.applicant_id)
            await user.send(f"🎉 Congratulations! Your **{self.app_config.get('app_name')}** application has been accepted!")
        except: pass

        await interaction.message.edit(view=None, content="✅ Application Accepted.")
        interaction.client.db["application_reviews"].update_one(
            {"message_id": interaction.message.id},
            {"$set": {"resolved": True}},
        )
        await interaction.response.defer()

    async def deny_btn(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        denied_ch = interaction.client.get_channel(self.app_config.get("denied_channel_id"))
        if denied_ch:
            new_embed = interaction.message.embeds[0]
            new_embed.color = discord.Color.red()
            new_embed.title = f"❌ Denied {self.app_config.get('app_name')} Application"
            new_embed.add_field(name="Denied By", value=interaction.user.mention, inline=False)
            await denied_ch.send(embed=new_embed)

        try:
            user = await interaction.client.fetch_user(self.applicant_id)
            await user.send(f"❌ Your **{self.app_config.get('app_name')}** application has been denied.")
        except: pass

        await interaction.message.edit(view=None, content="❌ Application Denied.")
        interaction.client.db["application_reviews"].update_one(
            {"message_id": interaction.message.id},
            {"$set": {"resolved": True}},
        )
        await interaction.response.defer()

    async def ticket_btn(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        guild = interaction.guild
        applicant = guild.get_member(self.applicant_id) or await guild.fetch_member(self.applicant_id)
        uname = applicant.name.lower()

        cat = discord.utils.get(guild.categories, name="Application Tickets")
        if not cat:
            cat = await guild.create_category("Application Tickets")
            await cat.set_permissions(guild.default_role, read_messages=False)

        trusted_role_name = get_guild_config(interaction.client.db, guild.id)["TRUSTED_STAFF_ROLE"]
        trusted_role = discord.utils.get(guild.roles, name=trusted_role_name)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            applicant: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        if trusted_role:
            overwrites[trusted_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True, attach_files=True)

        channel = await guild.create_text_channel(name=f"app-{uname}", category=cat, overwrites=overwrites, topic=f"Ticket by {applicant.name} | Application")

        from cogs.tickets_base import TicketView
        embed = discord.Embed(title="🎫 Application Ticket", description=f"### Discussion with {applicant.mention}\n\n━━━━━━━━━━━━━━━━━━", color=0x2b2d31, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Applicant", value=applicant.mention, inline=True)
        embed.add_field(name="Category", value="Application", inline=True)
        embed.set_footer(text=f"Channel ID: {channel.id}")

        view = TicketView()
        await channel.send(embed=embed, view=view)

        button = discord.utils.get(self.children, custom_id=f"app_ticket_{self.app_id}_{self.applicant_id}")
        if button:
            button.disabled = True
            button.label = "Ticket Opened"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Cog & DM Listener
# ---------------------------------------------------------------------------

class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            for app in bot.db["applications_config"].find():
                bot.add_view(ApplyPanelView(app["app_id"], app["app_name"]))
            for review in bot.db["application_reviews"].find({"resolved": False}):
                app_config = bot.db["applications_config"].find_one(
                    {"guild_id": review["guild_id"], "app_id": review["app_id"]}
                )
                if app_config:
                    bot.add_view(ApplicationActionView(review["app_id"], review["applicant_id"], app_config))
            print("✅ Loaded Application Panel views.")
        except Exception as e:
            print(f"❌ Failed to load application views: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only listen to DMs from humans
        if message.author.bot or message.guild is not None:
            return

        # Check for active application session in MongoDB
        session = self.bot.db["application_sessions"].find_one({"user_id": message.author.id})
        if not session:
            return # Not in an application flow

        content = message.content.strip()
        
        # Cancel command
        if content.lower() == "cancel":
            self.bot.db["application_sessions"].delete_one({"user_id": message.author.id})
            
            # Red Cancel Embed
            cancel_embed = discord.Embed(
                title="❌ Application Cancelled",
                description="You have cancelled your application. You can close this DM.",
                color=discord.Color.red()
            )
            await message.channel.send(embed=cancel_embed)
            return

        # Fetch app config
        app_config = self.bot.db["applications_config"].find_one({"guild_id": session["guild_id"], "app_id": session["app_id"]})
        if not app_config:
            self.bot.db["application_sessions"].delete_one({"user_id": message.author.id})
            await message.channel.send("❌ Error: Application config missing. Session cleared.")
            return

        # Save answer
        answers = session.get("answers", [])
        answers.append(content)
        current_q = session.get("current_q", 0)

        questions = app_config.get("questions", [])
        total_q = len(questions)
        next_q_index = current_q + 1

        if next_q_index < total_q:
            # Ask next question
            self.bot.db["application_sessions"].update_one(
                {"user_id": message.author.id},
                {"$set": {"current_q": next_q_index, "answers": answers}}
            )
            
            # Blue Question Embed
            q_embed = discord.Embed(
                title=f"{app_config['app_name']}",
                description=f"**{next_q_index + 1}/{total_q}. {questions[next_q_index]}**\n\nType your answer below.",
                color=discord.Color.blue()
            )
            await message.channel.send(embed=q_embed)
            
        else:
            # Finished! Submit application
            self.bot.db["application_sessions"].delete_one({"user_id": message.author.id})
            
            # Green Finish Embed
            finish_embed = discord.Embed(
                title="✅ Application Submitted",
                description=f"Your application for **{app_config['app_name']}** has been successfully submitted!\nYou can close this DM.",
                color=discord.Color.green()
            )
            await message.channel.send(embed=finish_embed)
            
            # Build Embed for Staff Channel
            embed = discord.Embed(title=f"📄 New {app_config['app_name']} Application", color=0x2b2d31, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Applicant", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
            embed.set_thumbnail(url=message.author.display_avatar.url)

            for i, q in enumerate(questions):
                if i < len(answers):
                    val = answers[i]
                    if len(val) > 1024: val = val[:1021] + "..."
                    embed.add_field(name=f"{i+1}/{total_q}. {q[:45]}", value=val, inline=False)

            # Send to submitted channel
            submitted_channel = self.bot.get_channel(app_config.get("submitted_channel_id"))
            if submitted_channel:
                trusted_role_name = get_guild_config(self.bot.db, submitted_channel.guild.id)["TRUSTED_STAFF_ROLE"]
                trusted_role = discord.utils.get(submitted_channel.guild.roles, name=trusted_role_name)
                mention = trusted_role.mention if trusted_role else f"@{trusted_role_name}"
                
                view = ApplicationActionView(session["app_id"], message.author.id, app_config)
                review_msg = await submitted_channel.send(content=mention, embed=embed, view=view)
                self.bot.db["application_reviews"].update_one(
                    {"message_id": review_msg.id},
                    {"$set": {
                        "guild_id": session["guild_id"],
                        "app_id": session["app_id"],
                        "applicant_id": message.author.id,
                        "resolved": False,
                    }},
                    upsert=True,
                )
                self.bot.add_view(view)

async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))