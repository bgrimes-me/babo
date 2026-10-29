// Babo's plugin for a resident's opencode. Installed by Babo into the resident's own
// opencode config folder at every session start; edits here are overwritten.
//
// It does as little as it can: it writes each tool call and each new message to a
// spool file Babo reads (Babo redacts and files them in the town's event log), and it
// calls `babo session checkpoint` when the harness should ask the resident to write a
// checkpoint in its own words: every so many tool calls, before a git commit, when the
// session goes quiet, and when the context is nearly full. It also gives every turn a
// clock the resident can trust, at the end of the conversation so the cached start of
// the prompt never changes.

import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

const env = process.env
const SPOOL = env.BABO_SPOOL
const EVERY = Number(env.BABO_CHECKPOINT_EVERY || 15)
const GATE = Number(env.BABO_COMMIT_GATE || 30)
const IDLE_MS = Number(env.BABO_IDLE_MINUTES || 20) * 60_000
const CONTEXT = Number(env.BABO_CONTEXT_TOKENS || 0)
const COMMIT = /(^|[\s;&|(])git\s+(-[cC]\s+\S+\s+)*commit\b/

export const Babo = async () => {
  let since = 0
  let idleTimer = null
  let busy = null
  let rotated = false

  const spool = (record) => {
    if (!SPOOL) return
    mkdirSync(dirname(SPOOL), { recursive: true })
    appendFileSync(SPOOL, JSON.stringify({ at: new Date().toISOString(), ...record }) + "\n")
  }

  const checkpoint = (trigger) => {
    if (busy) return busy
    since = 0
    const proc = Bun.spawn(
      [env.BABO_BIN, "session", "checkpoint", env.BABO_RESIDENT, env.BABO_SESSION, "--trigger", trigger, "--config", env.BABO_CONFIG],
      { stdout: "ignore", stderr: "ignore", stdin: "ignore" },
    )
    busy = proc.exited.finally(() => { busy = null })
    return busy
  }

  const quietLater = () => {
    if (idleTimer) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => { if (since > 0) checkpoint("idle") }, IDLE_MS)
  }

  return {
    "chat.message": async () => {
      spool({ kind: "turn_start" })
      if (idleTimer) clearTimeout(idleTimer)
    },
    "tool.execute.before": async (input, output) => {
      const command = input.tool === "bash" ? String(output?.args?.command ?? "") : ""
      // The commit gate: work isn't committed until its checkpoint is written.
      if (command && COMMIT.test(command) && since >= Math.min(GATE, EVERY)) await checkpoint("commit")
    },
    "tool.execute.after": async (input, output) => {
      const exit = output?.metadata?.exit
      spool({
        kind: "tool", tool: input.tool, args: input.args ?? {},
        output: String(output?.output ?? "").slice(0, 2000),
        exit_code: typeof exit === "number" ? exit : null,
      })
      since += 1
      if (since >= EVERY) checkpoint("interval")
    },
    "experimental.chat.messages.transform": async (_input, output) => {
      const last = output.messages[output.messages.length - 1]
      const text = last?.info?.role === "user" ? last.parts.find((p) => p.type === "text" && !p.synthetic) : null
      if (text && !text.text.includes("\n\nNow: ")) {
        const now = new Date()
        const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
        text.text += `\n\nNow: ${now.toLocaleString("en-GB", { weekday: "long", timeZone: zone })} (${zone}; the machine's clock)`
      }
    },
    event: async ({ event }) => {
      if (event.type === "session.idle") quietLater()
      if (event.type === "message.updated" && CONTEXT && !rotated) {
        const info = event.properties?.info
        const used = (info?.tokens?.input ?? 0) + (info?.tokens?.cache?.read ?? 0) + (info?.tokens?.output ?? 0)
        if (info?.role === "assistant" && info?.time?.completed && used > CONTEXT * 0.8) {
          rotated = true
          checkpoint("context_full")
        }
      }
    },
  }
}
