// Babo's plugin for a resident's opencode (opencode 2's plugin API). Installed by Babo
// into the resident's own opencode config folder at every session start; edits here are
// overwritten.
//
// It does as little as it can: it writes each tool call and each new message to a
// spool file Babo reads (Babo redacts and files them in the town's event log), and it
// calls `babo session checkpoint` when the harness should ask the resident to write a
// checkpoint in its own words: every so many tool calls, before a git commit, when the
// session goes quiet, and when the context is nearly full. It also gives every turn a
// clock the resident can trust, at the end of the conversation (only in what goes to the
// model) so the cached start of the prompt never changes.

import { spawn } from "node:child_process"
import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

const env = process.env
const SPOOL = env.BABO_SPOOL
const EVERY = Number(env.BABO_CHECKPOINT_EVERY || 15)
const GATE = Number(env.BABO_COMMIT_GATE || 30)
const IDLE_MS = Number(env.BABO_IDLE_MINUTES || 20) * 60_000
const CONTEXT = Number(env.BABO_CONTEXT_TOKENS || 0)
const COMMIT = /(^|[\s;&|(])git\s+(-[cC]\s+\S+\s+)*commit\b/

export default {
  id: "babo",
  async setup(ctx) {
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
      busy = new Promise((resolve) => {
        const proc = spawn(
          env.BABO_BIN,
          ["session", "checkpoint", env.BABO_RESIDENT, env.BABO_SESSION, "--trigger", trigger, "--config", env.BABO_CONFIG],
          { stdio: "ignore" },
        )
        proc.on("exit", resolve)
        proc.on("error", resolve)
      }).finally(() => { busy = null })
      return busy
    }

    const quietLater = () => {
      if (idleTimer) clearTimeout(idleTimer)
      idleTimer = setTimeout(() => { if (since > 0) checkpoint("idle") }, IDLE_MS)
    }

    // opencode lists an MCP server's tools a moment after it connects: the first turn waits
    // (briefly) for the town's tools, so the resident never starts a session without them.
    let townTools = false
    const waitForTownTools = async () => {
      const until = Date.now() + 30_000
      while (!townTools && Date.now() < until) {
        townTools = (await ctx.tool.list()).some((tool) => tool.id.startsWith("babo_"))
        if (!townTools) await new Promise((resolve) => setTimeout(resolve, 100))
      }
    }

    await ctx.session.hook("prompt", async () => {
      spool({ kind: "turn_start" })
      if (idleTimer) clearTimeout(idleTimer)
      await waitForTownTools()
    })

    await ctx.tool.hook("execute.before", async (event) => {
      const command = event.tool === "shell" ? String(event.input?.command ?? "") : ""
      // The commit gate: work isn't committed until its checkpoint is written.
      if (command && COMMIT.test(command) && since >= Math.min(GATE, EVERY)) await checkpoint("commit")
    })

    await ctx.tool.hook("execute.after", (event) => {
      const result = event.result ?? {}
      const text = (result.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join("\n")
      const exit = result.output?.exit ?? result.metadata?.exit
      spool({
        kind: "tool", tool: event.tool, args: event.input ?? {},
        output: String(event.status === "error" ? event.error?.message ?? "error" : text).slice(0, 2000),
        exit_code: typeof exit === "number" ? exit : null,
      })
      since += 1
      if (since >= EVERY) checkpoint("interval")
    })

    await ctx.session.hook("context", (event) => {
      const last = event.messages[event.messages.length - 1]
      const text = last?.role === "user" ? (last.content ?? []).find((p) => p.type === "text") : null
      if (text && !text.text.includes("\n\nNow: ")) {
        const now = new Date()
        const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
        text.text += `\n\nNow: ${now.toLocaleString("en-GB", { weekday: "long", timeZone: zone })} (${zone}; the machine's clock)`
      }
    })

    const controller = new AbortController()
    void (async () => {
      const types = ["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted", "session.step.ended"]
      for await (const event of ctx.event.subscribe({ types, signal: controller.signal })) {
        if (event.type.startsWith("session.execution.")) quietLater()
        if (event.type === "session.step.ended" && CONTEXT && !rotated) {
          const tokens = event.data?.tokens ?? {}
          const used = (tokens.input ?? 0) + (tokens.cache?.read ?? 0) + (tokens.output ?? 0)
          if (used > CONTEXT * 0.8) {
            rotated = true
            checkpoint("context_full")
          }
        }
      }
    })().catch(() => {})
    return () => {
      controller.abort()
      if (idleTimer) clearTimeout(idleTimer)
    }
  },
}
