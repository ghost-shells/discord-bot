import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import subprocess, sys, pathlib
import shutil
import threading
from dotenv import load_dotenv
from db import get_bot_token, get_db

# Import the Flask app from the root directory
from app import app as flask_app

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

COGS = [
    'cogs.stats',
    'cogs.vouches',
    'cogs.tickets',
    'cogs.moderation',
    'cogs.giveaway',
    'cogs.sticky',
    'cogs.building',            # new building system
    'cogs.reactionroles',
    'cogs.welcome',
    'cogs.automod',
    'cogs.logging',
    'cogs.autorole',
    'cogs.applications',
    'cogs.staff_utils',
    'cogs.afk',
    'cogs.mcpay',
    'cogs.mc_link',        # per-user Minecraft account linking (/link, /unlink, /mc)
    'cogs.invites',
    'cogs.embed_activity',  # /embed sender + /activity presence setter
    'cogs.promotion',       # /promote and /demote (Helper -> Mod -> Admin)
    'cogs.ai_chat',         # conversational AI — replies on @mention or reply-to-bot

]

class Bot(commands.Bot):
    async def setup_hook(self):
        # --- MongoDB Setup ---
        self.db = get_db()
        if self.db is None:
            print("❌ MONGO_URI environment variable is not set!")
        else:
            try:
                self.db.client.admin.command('ping')
                print("✅ Successfully connected to MongoDB!")
            except Exception as e:
                print(f"❌ Failed to connect to MongoDB: {e}")

        # Bind the global tree error handler
        self.tree.on_error = self.on_tree_error

        # ── Spawn MC bot subprocess ──────────────────────────────────────────
        mc_bot_path = pathlib.Path(__file__).parent / "mc-bot" / "index.js"
        node_path = shutil.which("node")

        if not node_path:
            print("⚠️  'node' not found in PATH — MC bot not started. Check nixpacks.toml.")
        elif not mc_bot_path.exists():
            print("⚠️  mc-bot/index.js not found — MC bot not started.")
        else:
            mc_env = os.environ.copy()
            mc_env["MC_BOT_PORT"] = "3001"

            mc_proc = subprocess.Popen(
                [node_path, str(mc_bot_path)],
                env=mc_env,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

            print(f"✅ MC bot subprocess started (PID {mc_proc.pid})")

        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f'✅ {cog} loaded!')
            except Exception as e:
                print(f'❌ Failed to load {cog}: {e}')

        # rest of your code...

        # --- Start Web Dashboard in Background ---
        def run_web():
            port = int(os.getenv("PORT", 5000))
            print(f"🌐 Starting Flask Dashboard on 0.0.0.0:{port}...")
            try:
                flask_app.run(
                    host='0.0.0.0',
                    port=port,
                    debug=False,
                    use_reloader=False
                )
            except Exception as e:
                print(f"❌ Flask failed to start: {e}")

        threading.Thread(target=run_web, daemon=True).start()

    # --- Global Error Handler ---
    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error)
        else:
            import traceback
            traceback.print_exception(
                type(error),
                error,
                error.__traceback__
            )
            msg = "❌ An unexpected error occurred while running this command."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


bot = Bot(command_prefix='!', intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f'✅ Bot online: {bot.user} (ID: {bot.user.id})')
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} global command(s): {[f"/{c.name}" for c in synced]}')
    except Exception as e:
        print(f'❌ Global sync failed: {e}')

@bot.command(name='sync')
@commands.has_permissions(administrator=True)
async def sync_commands(ctx):
    msg = await ctx.send('🔄 Wiping old commands, reloading cogs, and syncing...')
    try:
        bot.tree.clear_commands(guild=None)
        bot.tree.clear_commands(guild=ctx.guild)
        for cog in COGS:
            try:
                await bot.reload_extension(cog)
                print(f'✅ Reloaded {cog}')
            except Exception as e:
                print(f'❌ Reload failed for {cog}: {e}')
        synced = await bot.tree.sync()
        names = ', '.join(f'/{c.name}' for c in synced)
        await msg.edit(content=f"✅ Synced {len(synced)} global command(s):\n{names}")
    except Exception as e:
        await msg.edit(content=f'❌ Sync failed: {e}')

@bot.command(name='reload')
@commands.has_permissions(administrator=True)
async def reload_cog(ctx, cog: str = ''):
    targets = COGS if not cog else [cog]
    results = []
    for c in targets:
        try:
            await bot.reload_extension(c)
            results.append(f'✅ {c}')
        except Exception as e:
            results.append(f'❌ {c}: {e}')
    await ctx.send('\n'.join(results))

@bot.command(name='listcogs')
@commands.has_permissions(administrator=True)
async def list_cogs(ctx):
    loaded = list(bot.extensions.keys())
    await ctx.send(f"Loaded cogs: {', '.join(loaded) if loaded else 'None'}")

def main():
    token = get_bot_token()
    if not token:
        raise RuntimeError('DISCORD_BOT_TOKEN or DISCORD_TOKEN environment variable is not set.')
    async def run_bot():
        async with bot:
            await bot.start(token)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")

if __name__ == '__main__':
    main()