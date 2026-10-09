/**
 * Node harness for .opencode/plugins/brain.js — the desktop app runs plugins under NODE (no
 * `Bun` global), which is exactly what broke the first version. Same 5 assertions, Node runtime.
 * Run: node automation/test_brain_plugin.mjs
 */
import fs from "node:fs"
import path from "node:path"
import os from "node:os"
import { fileURLToPath } from "node:url"
import { DatabaseSync } from "node:sqlite"

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..") // repo root
const T = fs.mkdtempSync(path.join(os.tmpdir(), "brainnode-"))

fs.copyFileSync(path.join(SRC, "brain.py"), path.join(T, "brain.py"))
fs.cpSync(path.join(SRC, "automation"), path.join(T, "automation"), {
  recursive: true,
  filter: (p) =>
    !p.includes("__pycache__") &&
    !p.endsWith("state.db") &&
    !p.endsWith(".harvest_cursors.json") &&
    !p.endsWith(".brain_state.json") &&
    !p.endsWith(".brain_hook.log") &&
    !p.includes(".brain_transcripts"),
})

// the project has no package.json, so a bare `.js` import would be parsed as CommonJS by Node.
// Copy to .mjs (unambiguously ESM) — opencode's own loader transpiles the real file.
// harvest() calls agents.chat to distill lessons; the real agents.py needs the root .env +
// live gateways, which this sandbox doesn't copy. Stub it so the harness stays offline while
// still exercising the real path: session.idle -> JSONL export -> harvest -> cursor advance.
// (Without a stub, agents import fails and harvest deliberately does NOT move its cursor —
// a failed distill must be retried, never skipped.)
fs.writeFileSync(path.join(T, "agents.py"),
  'def chat(messages, **kw):\n    return \'{"lessons":[]}\', "stub"\n')

const plug = path.join(T, "brain.mjs")
fs.copyFileSync(path.join(SRC, ".opencode/plugins/brain.js"), plug)
const plugin = (await import(plug)).default

if (typeof globalThis.Bun !== "undefined") throw new Error("run me under NODE, not bun")

const transcript = [
  { info: { role: "user" }, parts: [{ type: "text", text: "status: clips rendered, thanks" }] },
  { info: { role: "assistant" }, parts: [{ type: "text", text: "got it, nothing durable here" }] },
]
const client = { session: { messages: async () => ({ data: transcript }) } }
const hooks = await plugin({ directory: T, client })
const fire = (event) => hooks.event({ event })
const part = (tool, state, sessionID) => ({
  type: "message.part.updated",
  properties: { part: { id: "p1", type: "tool", tool, sessionID, callID: "c1", state } },
})

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const rows = (sql) => {
  const file = path.join(T, "state.db")
  if (!fs.existsSync(file)) return []
  try {
    const d = new DatabaseSync(file)
    const out = d.prepare(sql).all()
    d.close()
    return out
  } catch {
    return []
  }
}
async function until(fn, ms = 30000, what = "") {
  const t0 = Date.now()
  for (;;) {
    const v = fn()
    if (v) return v
    if (Date.now() - t0 > ms) {
      const f = path.join(T, "state.db")
      let diag = fs.existsSync(f) ? JSON.stringify(rows("SELECT kind,detail FROM brain_log")) : "no db"
      const lg = path.join(T, "automation", ".brain_hook.log")
      if (fs.existsSync(lg)) diag += " | log: " + fs.readFileSync(lg, "utf8").slice(-400)
      throw new Error(`timeout waiting for ${what} | ${diag}`)
    }
    await sleep(150)
  }
}

// 1. sensing: success + failure + arg translation
await fire(part("edit", { status: "completed", input: { filePath: T + "/x.py" } }, "S1"))
await fire(part("bash", { status: "completed", input: { command: "python3 qc.py --selfcheck" } }, "S1"))
await fire(part("bash", { status: "error", input: { command: "ffmpeg -i missing.mp4" }, error: "boom" }, "S1"))
await fire(part("apply_patch", { status: "completed", input: { patchText: "*** Update File: automation/qc.py\n+x" } }, "S1"))
await fire(part("read", { status: "completed", input: { filePath: T + "/brain.py" } }, "S1"))

const sensed = await until(() => {
  const r = rows("SELECT kind, detail FROM brain_log WHERE processed=0 ORDER BY id")
  return r.length >= 4 ? r : null
}, 30000, "observe rows")
const kinds = sensed.map((r) => r.kind).sort()
if (JSON.stringify(kinds) !== JSON.stringify(["bash", "bash_fail", "edit", "edit"]))
  throw new Error("wrong kinds sensed: " + JSON.stringify(sensed))
if (!sensed.some((r) => r.kind === "edit" && r.detail === "x.py"))
  throw new Error("edit lost file_path: " + JSON.stringify(sensed))
if (!sensed.some((r) => r.kind === "edit" && (r.detail || "").includes("qc.py")))
  throw new Error("apply_patch path not extracted: " + JSON.stringify(sensed))
if (!sensed.find((r) => r.kind === "bash_fail").detail.includes("missing.mp4"))
  throw new Error("bash_fail lost its command")
console.log("OK (node): senses completed + failed tools, translates filePath / apply_patch")

// 2. stop/harvest
await fire({ type: "session.idle", properties: { sessionID: "S1" } })
const tfile = path.join(T, "automation", ".brain_transcripts", "S1.jsonl")
const jsonl = await until(() => (fs.existsSync(tfile) ? fs.readFileSync(tfile, "utf8") : null), 30000, "transcript")
const lines = jsonl.trim().split("\n").map((l) => JSON.parse(l))
if (lines.length !== 2 || lines[0].type !== "user") throw new Error("transcript shape: " + jsonl)
const cursor = await until(() => {
  try {
    return JSON.parse(fs.readFileSync(path.join(T, "automation", ".harvest_cursors.json"), "utf8"))
  } catch {
    return null
  }
}, 60000, "harvest cursor")
if (cursor.S1 !== 2) throw new Error("harvest cursor: " + JSON.stringify(cursor))
console.log("OK (node): session.idle exports JSONL -> brain.py harvest advances its cursor")

// 3. reflect the abandoned session when the next one starts
await fire(part("edit", { status: "completed", input: { filePath: T + "/other.py" } }, "S2"))
const note = await until(() => {
  const r = rows("SELECT session, detail FROM brain_log WHERE kind='session'")
  return r.length ? r[r.length - 1] : null
}, 30000, "reflect session note")
const n = JSON.parse(note.detail)
if (note.session !== "S1") throw new Error("reflected wrong session: " + note.session)
if (n.events < 4) throw new Error("too few events: " + note.detail)
console.log(`OK (node): switching sessions reflects the abandoned one (${n.events} events)`)

// 4. fail-open
const deadDir = fs.mkdtempSync(path.join(os.tmpdir(), "nobra-"))
const dead = await plugin({ directory: deadDir, client })
await dead.event({ event: part("edit", { status: "completed", input: { filePath: "/x" } }, "SX") })
await dead.event({ event: { type: "session.idle", properties: { sessionID: "SX" } } })
await sleep(500)
if (fs.existsSync(path.join(deadDir, "state.db"))) throw new Error("missing brain.py still ran")
fs.rmSync(deadDir, { recursive: true, force: true })
console.log("OK (node): missing brain.py is survived (fail-open)")

// 5. root discovery from a subagent cwd
const sub = await plugin({ directory: path.join(T, "automation", "sites"), client })
await sub.event({ event: part("edit", { status: "completed", input: { filePath: path.join(T, "stitched.py") } }, "S3") })
const found = await until(() => {
  const r = rows("SELECT detail FROM brain_log WHERE session='S3' AND kind='edit'")
  return r.length ? r : null
}, 30000, "subdir-rooted sensing")
if (found[0].detail !== "stitched.py") throw new Error("wrong root: " + JSON.stringify(found))
console.log("OK (node): finds the brain from a subagent's cwd")

// 6. no hook errors were logged anywhere along the way
const lg = path.join(T, "automation", ".brain_hook.log")
if (fs.existsSync(lg)) throw new Error("hook log has errors: " + fs.readFileSync(lg, "utf8"))
console.log("OK (node): zero brain errors logged across all steps")

fs.rmSync(T, { recursive: true, force: true })
console.log("\nALL OK under Node: opencode's actual plugin runtime")
process.exit(0) // the plugin's 5-min reflect timer would otherwise keep the loop alive
