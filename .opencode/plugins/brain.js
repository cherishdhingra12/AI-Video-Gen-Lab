/**
 * .opencode/plugins/brain.js — the local brain's hooks under opencode.
 *
 * opencode has NO declarative `hooks` config (that is Claude Code) — its hooks are plugin
 * callbacks. This file is the exact equivalent of .claude/settings.json, so the brain senses,
 * reflects and learns identically whichever tool drives the session:
 *
 *   Claude Code (.claude/settings.json)     opencode equivalent (this file)
 *   ------------------------------------    -------------------------------------------------
 *   PostToolUse Edit|Write|Bash  ->         event: message.part.updated, tool state 'completed'
 *   PostToolUseFailure           ->         the same event, tool state 'error'
 *   Stop (harvest, async)        ->         session.idle — the turn is over, distil new lessons
 *   SessionEnd (reflect)         ->         session.idle debounced by a quiet period, plus a
 *                                           catch-up reflect for an abandoned session when the
 *                                           next one starts (opencode has no session-end event)
 *
 * Tool names are translated into the brain's vocabulary (Edit/Write/Bash) and `filePath` into
 * `file_path`, so brain.py stays a single front door with ONE sensing code path for both tools.
 *
 * Everything here is fail-open: a brain that errors, hangs or is missing must never block,
 * crash or slow a session. Never throw out of a hook.
 */
import fs from "node:fs"
import path from "node:path"
import { spawn } from "node:child_process"

// opencode's mutating tools -> the tool_name brain.observe() understands (Claude's matcher was
// Edit|Write|NotebookEdit|Bash; opencode has no notebooks, and apply_patch writes files too).
const SENSE_TOOLS = { edit: "Edit", write: "Write", apply_patch: "Edit", bash: "Bash" }

// A session is "over" once this long passes with no tool activity — opencode fires no
// session-end event, so a quiet period is the closest faithful stand-in for SessionEnd.
const QUIET_MS = 5 * 60 * 1000

export default async ({ directory, worktree, client }) => {
  // opencode's `directory` is the process cwd, which a subagent can set elsewhere — find the
  // brain by walking up (a brain lives where brain.py + automation/ sit together).
  const findRoot = (start) => {
    let dir = start
    for (let i = 0; i < 6 && dir && dir !== path.dirname(dir); i++) {
      if (fs.existsSync(path.join(dir, "brain.py")) && fs.existsSync(path.join(dir, "automation")))
        return dir
      dir = path.dirname(dir)
    }
    return null
  }
  const projectRoot =
    findRoot(directory) || (worktree && findRoot(worktree)) || directory || "."

  const brainPy = path.join(projectRoot, "brain.py")
  const aut = path.join(projectRoot, "automation")
  const stateFile = path.join(aut, ".brain_state.json") // last-session pointer (survives restart)
  const transcriptDir = path.join(aut, ".brain_transcripts")
  const ready = fs.existsSync(brainPy)

  // ---- fire-and-forget: the brain senses in the background, never in the way -----------------
  // stdout is noise, but stderr must NOT be discarded — an error that vanishes is how a sense
  // stream dies quietly. It lands in a capped log the owner can actually read.
  const logFile = path.join(aut, ".brain_hook.log")
  const LOG_MAX = 512 * 1024
  const log = (msg) => {
    try {
      fs.mkdirSync(aut, { recursive: true })
      if (fs.existsSync(logFile) && fs.statSync(logFile).size > LOG_MAX) {
        const tail = fs.readFileSync(logFile, "utf8").slice(-64 * 1024)
        fs.writeFileSync(logFile, tail)
      }
      fs.appendFileSync(logFile, `${new Date().toISOString()} ${msg.trim()}\n`)
    } catch {
      /* logging must never be the thing that fails */
    }
  }

  // Which interpreter runs the brain. opencode's app PATH is not your shell's PATH, so resolve a
  // real file rather than trusting `python3` — brain.py's chain is stdlib-only, so any of these
  // can sense/reflect/harvest (numpy/PIL are only needed by `brain.py review`, a human command).
  const PYTHON =
    process.env.PYTHON ||
    [
      "/usr/local/bin/python3",
      "/opt/homebrew/bin/python3",
      "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
      "/usr/bin/python3",
    ].find((p) => fs.existsSync(p)) ||
    "python3"

  const brain = (cmd, argv, payload) => {
    if (!ready) return
    try {
      // node:child_process — NOT Bun.spawn: opencode's desktop app runs plugins under Node,
      // where the Bun global does not exist (that silently killed every sensed event).
      const p = spawn(PYTHON, [brainPy, cmd, ...(argv || [])], {
        cwd: projectRoot,
        stdio: ["pipe", "ignore", "pipe"],
      })
      p.stdin.on("error", () => {}) // brain may exit before stdin drains
      p.stdin.end(JSON.stringify(payload || {}))
      let err = ""
      p.stderr.on("data", (d) => (err += d))
      p.on("close", (code) => {
        const e = err.trim()
        if (e) log(`brain.py ${cmd} (exit ${code}): ${e}`) // success prints nothing
      })
      p.on("error", (e) => log(`spawn ${cmd} failed: ${e && e.message}`))
      p.unref?.() // sensing never holds the app open
    } catch (e) {
      log(`spawn ${cmd} threw: ${e && e.message}`)
    }
  }

  // ---- transcript: brain.harvest() reads JSONL turns, opencode keeps messages in its SDK -----
  const writeTranscript = async (sessionID) => {
    const res = await client.session.messages({ path: { id: sessionID } })
    const rows = res && res.data
    if (!Array.isArray(rows) || !rows.length) return null
    const lines = []
    for (const row of rows) {
      const role = row && row.info && row.info.role
      if (role !== "user" && role !== "assistant") continue
      const text = (row.parts || [])
        .filter((p) => p && p.type === "text" && typeof p.text === "string")
        .map((p) => p.text)
        .join(" ")
        .trim()
      if (text) lines.push(JSON.stringify({ type: role, message: { content: text } }))
    }
    if (!lines.length) return null
    fs.mkdirSync(transcriptDir, { recursive: true })
    const file = path.join(transcriptDir, `${sessionID}.jsonl`)
    fs.writeFileSync(file, lines.join("\n") + "\n") // full rewrite: the line cursor only grows
    return file
  }

  const harvesting = new Set()
  const harvest = (sessionID) => {
    if (!ready || harvesting.has(sessionID)) return // one in-flight harvest per session
    harvesting.add(sessionID)
    writeTranscript(sessionID)
      .then((file) => file && brain("harvest", [], { transcript_path: file, session_id: sessionID }))
      .catch(() => {})
      .finally(() => harvesting.delete(sessionID))
  }

  // ---- reflect: distil a session's raw events once it goes quiet -----------------------------
  const readLast = () => {
    try {
      return JSON.parse(fs.readFileSync(stateFile, "utf8")).session || null
    } catch {
      return null
    }
  }
  const writeLast = (sid) => {
    try {
      fs.mkdirSync(path.dirname(stateFile), { recursive: true })
      fs.writeFileSync(stateFile, JSON.stringify({ session: sid, ts: Date.now() }))
    } catch {
      /* pointer is a nicety, not a requirement */
    }
  }

  let lastSession = readLast()
  const timers = new Map()

  const touch = (sessionID) => {
    if (!sessionID) return
    if (lastSession && lastSession !== sessionID) {
      // the previous session was abandoned before its quiet timer fired — distil it now
      brain("reflect", [], { session_id: lastSession })
    }
    if (lastSession !== sessionID) {
      lastSession = sessionID
      writeLast(sessionID)
    }
    // any activity pushes the quiet timer back, so reflect only ever sees a still session
    const prior = timers.get(sessionID)
    if (prior) clearTimeout(prior)
    timers.set(
      sessionID,
      setTimeout(() => {
        timers.delete(sessionID)
        brain("reflect", [], { session_id: sessionID })
      }, QUIET_MS),
    )
  }

  // ---- the one hook: the tool event stream carries success AND failure -----------------------
  const onToolPart = (part) => {
    if (!part || part.type !== "tool" || !part.state) return
    const status = part.state.status
    if (status !== "completed" && status !== "error") return // only terminal states
    touch(part.sessionID)

    const tool = SENSE_TOOLS[part.tool]
    if (!tool) return // read/grep/glob/... are not "changes" — same scope as Claude's matcher
    const input = { ...(part.state.input || {}) }
    if (input.filePath && !input.file_path) input.file_path = input.filePath
    if (!input.file_path && typeof input.patchText === "string") {
      // apply_patch carries no filePath — the path lives in the patch markers
      const m = input.patchText.match(/^\*\*\* (?:Add|Update|Move|Delete) File: (.+)$/m)
      if (m) input.file_path = m[1].trim()
    }
    brain("observe", status === "error" ? ["fail"] : [], {
      tool_name: tool,
      tool_input: input,
      session_id: part.sessionID,
    })
  }

  return {
    event: async ({ event }) => {
      try {
        const type = event && event.type
        if (type === "message.part.updated") {
          onToolPart(event.properties && event.properties.part)
        } else if (type === "session.idle") {
          const sid = event.properties && event.properties.sessionID
          touch(sid)
          harvest(sid) // Claude's Stop hook: distil new craft lessons after every turn
        }
      } catch {
        /* fail-open */
      }
    },
  }
}
