// mc-bot/index.js
const mineflayer      = require('mineflayer')
const express         = require('express')
const { MongoClient } = require('mongodb')
const fs              = require('fs')
const path            = require('path')

const app = express()
app.use(express.json())

// ── MongoDB ───────────────────────────────────────────────────────────────────
const mongoClient = new MongoClient(process.env.MONGO_URI)
let db = null

async function connectMongo() {
  await mongoClient.connect()
  db = mongoClient.db('discord_bot')
  console.log('[MC-BOT] ✅ Connected to MongoDB')
}

async function saveSession(data) {
  await db.collection('mc_auth').updateOne(
    { _id: 'ms_session' },
    { $set: { data, updated_at: new Date() } },
    { upsert: true }
  )
  console.log('[MC-BOT] 💾 Session saved to MongoDB')
}

async function loadSession() {
  const doc = await db.collection('mc_auth').findOne({ _id: 'ms_session' })
  return doc?.data ?? null
}

async function clearSession() {
  await db.collection('mc_auth').deleteOne({ _id: 'ms_session' })
  console.log('[MC-BOT] 🗑️  Session cleared')
}

// ── Profiles folder (mineflayer caches MS tokens here as JSON files) ──────────
const PROFILES_DIR = path.join(__dirname, '.mc_profiles')
if (!fs.existsSync(PROFILES_DIR)) fs.mkdirSync(PROFILES_DIR)

// Save mineflayer's profiles folder contents to MongoDB after login
async function backupProfiles() {
  try {
    const files = {}
    for (const f of fs.readdirSync(PROFILES_DIR)) {
      files[f] = fs.readFileSync(path.join(PROFILES_DIR, f), 'utf8')
    }
    if (Object.keys(files).length > 0) {
      await db.collection('mc_auth').updateOne(
        { _id: 'ms_profiles' },
        { $set: { files, updated_at: new Date() } },
        { upsert: true }
      )
      console.log('[MC-BOT] 💾 MS profiles backed up to MongoDB')
    }
  } catch (e) {
    console.error('[MC-BOT] Failed to backup profiles:', e.message)
  }
}

// Restore profiles from MongoDB to disk before connecting
async function restoreProfiles() {
  try {
    const doc = await db.collection('mc_auth').findOne({ _id: 'ms_profiles' })
    if (!doc?.files) return false
    for (const [name, content] of Object.entries(doc.files)) {
      fs.writeFileSync(path.join(PROFILES_DIR, name), content, 'utf8')
    }
    console.log('[MC-BOT] ✅ MS profiles restored from MongoDB')
    return true
  } catch (e) {
    console.error('[MC-BOT] Failed to restore profiles:', e.message)
    return false
  }
}

async function clearProfiles() {
  try {
    for (const f of fs.readdirSync(PROFILES_DIR)) {
      fs.unlinkSync(path.join(PROFILES_DIR, f))
    }
    await db.collection('mc_auth').deleteOne({ _id: 'ms_profiles' })
  } catch (e) {
    console.error('[MC-BOT] Failed to clear profiles:', e.message)
  }
}

// ── Facing direction (yaw/pitch) ───────────────────────────────────────────────
// Minecraft is supposed to remember where you were last facing, but on a bot
// rejoin that can get reset. We track it ourselves and force it back on spawn.
async function saveLook(yaw, pitch) {
  try {
    await db.collection('mc_auth').updateOne(
      { _id: 'mc_look' },
      { $set: { yaw, pitch, updated_at: new Date() } },
      { upsert: true }
    )
  } catch (e) {
    console.error('[MC-BOT] Failed to save look:', e.message)
  }
}

async function loadLook() {
  try {
    const doc = await db.collection('mc_auth').findOne({ _id: 'mc_look' })
    return doc ? { yaw: doc.yaw, pitch: doc.pitch } : null
  } catch (e) {
    console.error('[MC-BOT] Failed to load look:', e.message)
    return null
  }
}

// ── State ─────────────────────────────────────────────────────────────────────
let bot              = null
let botReady         = false
let manualDisconnect = false
let lookSaveInterval = null    // periodically persists current yaw/pitch while connected

// status: disconnected | awaiting_auth | awaiting_discord_auth | connecting | ready | error
let state = { status: 'disconnected', code: null, url: null, error: null }

function setState(patch) {
  state = { ...state, ...patch }
  console.log(`[MC-BOT] status → ${state.status}${state.code ? ` code=${state.code}` : ''}`)
}

// ── Bot ───────────────────────────────────────────────────────────────────────
let reconnectTimer = null

function scheduleReconnect(ms = 15000) {
  if (reconnectTimer) return
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null
    // Always try to restore saved profiles — never trigger a fresh MS login
    // on an automatic reconnect (the user didn't ask to re-authenticate)
    const hasProfiles = await restoreProfiles()
    if (!hasProfiles) {
      // No saved session at all — stay disconnected, don't spam MS device-code
      console.log('[MC-BOT] No saved profiles to reconnect with — staying disconnected.')
      setState({ status: 'disconnected', code: null, url: null, error: null })
      return
    }
    startBot(true)
  }, ms)
}

function startBot(hasProfiles = false) {
  if (bot) { try { bot.end() } catch (_) {} }
  bot      = null
  botReady = false
  setState({ status: 'connecting', code: null, url: null, error: null })

  const opts = {
    host:           process.env.MC_SERVER_HOST || 'play.donutsmp.net',
    version:        process.env.MC_VERSION     || '1.21',
    auth:           'microsoft',
    profilesFolder: PROFILES_DIR,  // mineflayer reads/writes cached MS tokens here
  }

  // Only set up device-code flow when we have NO saved session
  // If hasProfiles is true, mineflayer will silently reuse the cached token
  if (!hasProfiles) {
    opts.onMsaCode = ({ user_code, verification_uri }) => {
      console.log(`[MC-BOT] 🔑 Device code: ${user_code}`)
      console.log(`[MC-BOT] 🔗 URL: ${verification_uri}`)
      setState({
        status: 'awaiting_auth',
        code:   user_code,
        url:    verification_uri,
        error:  null,
      })
    }
  } else {
    console.log('[MC-BOT] 🔄 Using cached MS token — no login required')
  }

  bot = mineflayer.createBot(opts)

  bot.on('spawn', async () => {
    botReady = true
    setState({ status: 'ready', code: null, url: null, error: null })
    // Backup the profiles folder to MongoDB so they survive restarts
    await backupProfiles()

    // Force-restore the facing direction from before we last disconnected —
    // the server can reset rotation on rejoin otherwise.
    try {
      const saved = await loadLook()
      if (saved && bot) {
        bot.look(saved.yaw, saved.pitch, true)
        console.log('[MC-BOT] 🧭 Restored facing direction')
      }
    } catch (e) {
      console.error('[MC-BOT] Failed to restore look:', e.message)
    }

    // Keep persisting current facing direction while connected, so the next
    // reconnect (restart, etc.) knows where to face.
    if (lookSaveInterval) clearInterval(lookSaveInterval)
    lookSaveInterval = setInterval(() => {
      if (bot && bot.entity) saveLook(bot.entity.yaw, bot.entity.pitch)
    }, 5000)
  })

  bot.on('kicked', (reason) => {
    const msg = typeof reason === 'object' ? JSON.stringify(reason) : String(reason)
    console.log(`[MC-BOT] Kicked: ${msg}`)
    botReady = false
    const isAuthKick = msg.toLowerCase().includes('discord') ||
                       msg.toLowerCase().includes('verify')  ||
                       msg.toLowerCase().includes('authoriz')
    if (isAuthKick) {
      setState({ status: 'awaiting_discord_auth', code: null, url: null, error: null })
    }
  })

  bot.on('error', (err) => {
    console.error('[MC-BOT] Error:', err.message)
    botReady = false
    setState({ status: 'error', code: null, url: null, error: err.message })
  })

  bot.on('end', (reason) => {
    console.log(`[MC-BOT] Disconnected: ${reason}`)
    botReady = false
    if (lookSaveInterval) { clearInterval(lookSaveInterval); lookSaveInterval = null }
    if (manualDisconnect) {
      // User explicitly left — stay disconnected, don't auto-reconnect
      setState({ status: 'disconnected', code: null, url: null, error: null })
      return
    }
    if (state.status === 'awaiting_discord_auth') {
      // Don't reconnect — waiting for user to click "I Authorized"
      return
    }
    // Unexpected drop — schedule reconnect using saved MS profiles
    setState({ status: 'disconnected', code: null, url: null, error: null })
    scheduleReconnect(15000)
  })
}

// ── Routes ────────────────────────────────────────────────────────────────────

app.get('/status', (_req, res) => res.json(state))

// Connect — reuse saved profiles if available, only do fresh MS login if none exist
app.post("/start-login", async (_req, res) => {
  if (botReady) return res.json({ ok: true, message: "Already connected" })
  manualDisconnect = false
  const hasProfiles = await restoreProfiles()
  if (hasProfiles) {
    console.log("[MC-BOT] 🔄 Saved session found — reconnecting silently...")
    startBot(true)
  } else {
    console.log("[MC-BOT] 🔑 No saved session — starting fresh MS login...")
    startBot(false)
  }
  res.json({ ok: true })
})

// "I Authorized" button — reconnect using saved profiles
app.post('/reconnect', async (_req, res) => {
  console.log('[MC-BOT] Manual reconnect triggered')
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  manualDisconnect = false
  setState({ status: 'connecting', code: null, url: null, error: null })
  setTimeout(async () => {
    const hasProfiles = await restoreProfiles()
    startBot(hasProfiles)
  }, 2000)
  res.json({ ok: true })
})

// Leave server — keep profiles/token saved
app.post('/logout', async (_req, res) => {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  manualDisconnect = true
  // Backup profiles + current facing direction to MongoDB before ending so they survive
  await backupProfiles()
  if (bot?.entity) await saveLook(bot.entity.yaw, bot.entity.pitch)
  try { bot?.end() } catch (_) {}
  bot      = null
  botReady = false
  setState({ status: 'disconnected', code: null, url: null, error: null })
  res.json({ ok: true })
})

// Full sign out — clear everything
app.post('/full-logout', async (_req, res) => {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  manualDisconnect = true
  await clearProfiles()
  try { bot?.end() } catch (_) {}
  bot      = null
  botReady = false
  setState({ status: 'disconnected', code: null, url: null, error: null })
  res.json({ ok: true })
})

// Run in-game command
app.post('/run-command', (req, res) => {
  const { command } = req.body
  if (!command || typeof command !== 'string')
    return res.status(400).json({ ok: false, error: 'Missing command' })
  if (!botReady || !bot)
    return res.status(503).json({ ok: false, error: 'MC bot not ready' })
  try {
    bot.chat(command)
    console.log(`[MC-BOT] ▶ ${command}`)
    res.json({ ok: true })
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message })
  }
})

// ── Start ─────────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.MC_BOT_PORT || '3001')

connectMongo().then(async () => {
  app.listen(PORT, '127.0.0.1', () =>
    console.log(`[MC-BOT] 🌐 Listening on 127.0.0.1:${PORT}`)
  )
  // On boot, only restore the cached MS token from MongoDB to disk —
  // do NOT auto-connect to the server. Joining is a manual action now
  // (dashboard "Connect" button / /mc-start-login), so a process
  // restart doesn't silently put the bot back on the server.
  const hasProfiles = await restoreProfiles()
  if (hasProfiles) {
    console.log('[MC-BOT] 🔄 Session restored — staying disconnected until manually connected.')
  } else {
    console.log('[MC-BOT] ℹ️  No saved session — use the dashboard to log in.')
  }
  setState({ status: 'disconnected', code: null, url: null, error: null })
}).catch(err => {
  console.error('[MC-BOT] ❌ MongoDB connection failed:', err)
  process.exit(1)
})