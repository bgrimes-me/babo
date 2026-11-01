// Babo's plugin for a resident's opencode (opencode 2's plugin API). Installed by Babo
// into the resident's own opencode config folder at every session start; edits here are
// overwritten.
//
// It does as little as it can: it writes each tool call and each new message to a
// spool file Babo reads (Babo redacts and files them in the town's event log), and it
// calls `babo session checkpoint` when the harness should ask the resident to write a
// checkpoint in its own words: every so many tool calls, before a git commit, when the
// session goes quiet, and when the context is nearly full. At the end of the conversation
// (only in what goes to the model, so the cached start of the prompt never changes) it adds
// where the resident is now (its baton and letters, kept up to date by Babo) and a clock.

import { spawn } from "node:child_process"
import { appendFileSync, mkdirSync, readFileSync, realpathSync } from "node:fs"
import { dirname } from "node:path"

const env = process.env
const SPOOL = env.BABO_SPOOL
const EVERY = Number(env.BABO_CHECKPOINT_EVERY || 15)
const GATE = Number(env.BABO_COMMIT_GATE || 30)
const IDLE_MS = Number(env.BABO_IDLE_MINUTES || 20) * 60_000
const CONTEXT = Number(env.BABO_CONTEXT_TOKENS || 0)
const NOW = env.BABO_NOW
const GUIDES = env.BABO_GUIDES
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
    // briefly for the town's tools, so the resident doesn't start a session without them. It
    // waits only while the server is connected and its tools are on their way, never for long,
    // and only once: a message must never be held up by tools that aren't coming.
    let waited = false
    const within = (promise, ms) => Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve(null), ms))])
    const waitForTownTools = async () => {
      if (waited) return
      waited = true
      const until = Date.now() + 8_000
      while (Date.now() < until) {
        const tools = (await within(ctx.tool.list(), 2_000)) ?? []
        if (tools.some((tool) => tool.id.startsWith("babo_"))) return
        const servers = (await within(ctx.mcp.list(), 2_000))?.data ?? []
        const town = servers.find((server) => server.name === "babo")
        if (!town || town.status?.status !== "connected" && town.status?.status !== "pending") return
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
    }

    // The resident works in its own folder. A session opened somewhere else (say, opencode's web
    // page opening the whole town) would see paths from there, and the edit fence matches paths
    // relative to where a session works; so such a session may read, but never change anything.
    const real = (path) => { try { return realpathSync(path) } catch { return path } }
    const HOME = env.BABO_HOME ? real(env.BABO_HOME) : ""
    const where = new Map()
    const homeOf = async (sessionID) => {
      if (!where.has(sessionID)) {
        const session = await within(ctx.session.get({ sessionID }), 5_000)
        const directory = session?.data?.location?.directory ?? session?.location?.directory
        where.set(sessionID, directory ? real(directory) : null)
      }
      return where.get(sessionID)
    }
    await ctx.permission.hook("evaluate", async (event) => {
      if (!HOME || ["read", "glob", "grep"].includes(event.action)) return
      const directory = await homeOf(event.sessionID).catch(() => null)
      if (directory !== HOME) {
        event.effect = "deny"
        event.message = `This resident changes things only in sessions that work in its own folder (${HOME}). Start one from Babo: the Sessions tab, or babo session new.`
      }
    })

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

    // Guides Babo brings in when they're needed: with the result of the first call of a tool in
    // this session, or after a call that was refused or failed in a way a guide covers. Once each.
    // (A refused call's result is opencode's own, so that guide goes with the turn, beside the baton.)
    let guides = null
    const shownGuides = new Set()
    const withTurn = []
    const loadGuides = () => {
      if (guides === null) { try { guides = GUIDES ? JSON.parse(readFileSync(GUIDES, "utf8")) : [] } catch { guides = [] } }
      return guides
    }
    const toolName = (name) => { const bare = String(name).replace(/^babo_/, ""); return { bash: "shell", exec: "shell" }[bare] ?? bare }
    const dueGuides = (tool, text, failed) => {
      const out = []
      for (const guide of loadGuides()) {
        if (shownGuides.has(guide.name)) continue
        const how = (guide.tools ?? []).includes(toolName(tool)) ? `tool:${toolName(tool)}`
          : failed && (guide.errors ?? []).some((pattern) => { try { return new RegExp(pattern.replace(/^\(\?i\)/, ""), pattern.startsWith("(?i)") ? "i" : "").test(text) } catch { return false } }) ? "error" : ""
        if (how) { shownGuides.add(guide.name); out.push(guide); spool({ kind: "guide", name: guide.name, how }) }
      }
      return out
    }

    await ctx.tool.hook("execute.after", (event) => {
      const said = event.status === "error" ? String(event.error?.message ?? "") + " " + JSON.stringify(event.error?.error ?? {})
        : (event.result?.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join("\n")
      for (const guide of dueGuides(event.tool, said, event.status === "error")) {
        if (event.status !== "error" && event.result) (event.result.content ??= []).push({ type: "text", text: guide.text })
        else withTurn.push(guide.text)
      }
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

    // The end of what goes to the model (never the stored conversation, so the cached start of the
    // prompt never changes): where the resident is now (its baton, unread letters; Babo keeps it up to
    // date), then a clock it can trust.
    const nowText = () => { try { return NOW ? readFileSync(NOW, "utf8").trim() : "" } catch { return "" } }
    await ctx.session.hook("context", (event) => {
      // The turn's own message (the last one from the person), on every step of the turn, so each
      // step sends the same text there and the cache holds through the turn's tool calls too.
      const last = event.messages.findLast((m) => m.role === "user")
      const text = last ? (last.content ?? []).find((p) => p.type === "text") : null
      if (text && !text.text.includes("\n\nNow: ")) {
        const where = nowText()
        if (where) text.text += `\n\n${where}`
        const now = new Date()
        const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
        text.text += `\n\nNow: ${now.toLocaleString("en-GB", { weekday: "long", timeZone: zone })} (${zone}; the machine's clock)`
      }
      // A guide that came after a refusal: once it is in, it stays (the same text on every later step).
      if (text) for (const guide of withTurn) if (!text.text.includes(guide)) text.text += `\n\n${guide}`
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
