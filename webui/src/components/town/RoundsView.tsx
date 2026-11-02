import { useCallback, useEffect, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { useClient } from "@/providers/ClientProvider";
import { SessionAccessFields } from "@/components/town/AccessFields";
import {
  approveTownRehearsal, fetchTownRounds, rehearseTownRound, runTownRound, setTownRound, setTownRoundAccess,
  type TownRehearsal, type TownRound, type TownRoundStep, type TownRounds,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const ROUNDS_POLL_MS = 10_000;

/** The town's work, in rounds: when each runs, its steps by office (and who holds each), its last run with each
 *  step's report; and Babo's own work, which always runs. The same as `babo rounds list|show|run|set`. */
export function RoundsView({ onNotice, onError }: { onNotice: (message: string) => void; onError: (message: string) => void }) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [data, setData] = useState<TownRounds | null>(null);
  const [busy, setBusy] = useState("");
  const load = useCallback(async () => {
    try { setData(await fetchTownRounds(getToken())); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  }, [getToken, onError]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, ROUNDS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);
  if (!data) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;

  const act = async (name: string, work: () => Promise<TownRounds & { message: string }>) => {
    setBusy(name);
    try { const next = await work(); setData(next); onNotice(next.message); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  };
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.roundsIntro", { defaultValue: "The town's work is done in rounds: when each one runs, and its steps in order, each an office's duty. A step for an office nobody holds waits until someone moves in. Same as `babo rounds`." })}
    </p>
    {Object.entries(data.broken).map(([name, why]) => <p key={name} role="alert" className="settings-list-inset text-[13px] text-destructive">
      {t("town.roundBroken", { defaultValue: "rounds/{{name}}.md can't be read: {{why}}", name, why })}
    </p>)}
    {data.rounds.map((round) => <RoundCard key={round.name} round={round} busy={busy === round.name}
      onRun={() => void act(round.name, () => runTownRound(client, round.name))}
      onSet={(change) => void act(round.name, () => setTownRound(client, round.name, change))}
      onRehearse={(live) => void act(round.name, () => rehearseTownRound(client, round.name, live))}
      onApprove={(id) => void act(round.name, () => approveTownRehearsal(client, round.name, id))}
      onAccess={(step, access, commands) => void act(round.name, () => setTownRoundAccess(client, round.name, step, access, commands))} />)}
    <section aria-label={t("town.baboOwn", { defaultValue: "Part of Babo" })} className="mt-4">
      <h2 className="settings-list-inset text-[13px] font-medium text-foreground">{t("town.baboOwn", { defaultValue: "Part of Babo" })}</h2>
      <p className="settings-list-inset text-xs text-muted-foreground">{t("town.baboOwnIntro", { defaultValue: "What Babo does itself, for every resident, whatever the rounds: upkeep." })}</p>
      <SettingsGroup>
        {data.babo.map((b) => <div key={b.title} className="settings-list-row py-2 text-[13px]"><span className="font-medium">{b.title}</span>
          <span className="text-muted-foreground"> · {b.what}</span></div>)}
      </SettingsGroup>
    </section>
  </>;
}

function RoundCard({ round, busy, onRun, onSet, onRehearse, onApprove, onAccess }: {
  round: TownRound; busy: boolean; onRun: () => void; onSet: (change: { enabled?: boolean; runs?: "wait" | "now" }) => void;
  onRehearse: (live: boolean) => void; onApprove: (rehearsal: string) => void;
  onAccess: (step: number, access: Record<string, string>, commands?: string[]) => void;
}) {
  const { t } = useTranslation();
  const [last] = round.recent;
  return <section aria-label={round.title} className="mt-3">
    <h2 className="settings-list-inset flex flex-wrap items-center gap-2 text-[14px] font-medium text-foreground">
      {round.title}<span className="text-xs font-normal text-muted-foreground">{round.name}</span>
      {!round.enabled && <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-normal text-muted-foreground">{t("town.roundOff", { defaultValue: "off" })}</span>}
    </h2>
    {round.about && <p className="settings-list-inset text-xs text-muted-foreground">{round.about}</p>}
    <SettingsGroup>
      <div className="settings-list-row flex flex-wrap items-center gap-x-4 gap-y-2 py-3 text-[13px]">
        <span className="min-w-[14rem] flex-1">
          <span className="block">{t("town.roundWhen", { defaultValue: "Runs {{when}}", when: round.when })}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">
            {round.last_run ? t("town.roundLast", { defaultValue: "last run {{when}}: {{status}}", when: round.last_run.slice(0, 16).replace("T", " "), status: round.last_status }) : t("town.roundNever", { defaultValue: "not run yet" })}
            {round.next_run ? ` · ${t("town.roundNext", { defaultValue: "next {{when}}", when: round.next_run.replace("T", " ") })}` : ""}
          </span>
        </span>
        <label className="flex items-center gap-2 text-xs">
          {t("town.roundRuns", { defaultValue: "When it's due" })}
          <select className="rounded border bg-background px-1 py-0.5 text-[13px]" value={round.runs} disabled={busy}
            onChange={(event) => onSet({ runs: event.target.value as "wait" | "now" })}>
            <option value="wait">{t("town.roundWait", { defaultValue: "waits for a free model" })}</option>
            <option value="now">{t("town.roundNow", { defaultValue: "runs right away" })}</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" role="switch" aria-label={t("town.roundOn", { defaultValue: "On" })} checked={round.enabled} disabled={busy}
            onChange={(event) => onSet({ enabled: event.target.checked })} />
          {t("town.roundOn", { defaultValue: "On" })}
        </label>
        <Button size="sm" variant="secondary" disabled={busy || !round.enabled} onClick={onRun} className="gap-1.5">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}{t("town.runRound", { defaultValue: "Run now" })}
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onRehearse(false)}>{t("town.rehearse", { defaultValue: "Rehearse" })}</Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onRehearse(true)}>{t("town.rehearseLive", { defaultValue: "Rehearse live" })}</Button>
        <p className="basis-full text-xs text-muted-foreground">
          {t("town.rehearsalIsTraining", { defaultValue: "A rehearsal is a training run: the residents really do the work, and learn from it. Letters, notes and posts are sent, and anything you allow really runs; only what each resident changes in its own folder is put back afterwards." })}
        </p>
        {round.runs === "now" && <p role="note" className="basis-full text-xs text-amber-600 dark:text-amber-400">
          {t("town.roundNowWarning", { defaultValue: "It won't wait for you to finish: while it runs, it shares the model with your chats and can slow them down (on a local model, noticeably)." })}
        </p>}
      </div>
      {round.steps.map((step, index) => {
        const ran = last?.steps[index];
        return <div key={index} className="settings-list-row py-2.5 text-[13px]">
          <span className="block">
            <span className="font-medium">{index + 1}. {step.office}</span>
            <span className={cn("ml-1.5 text-xs", step.holder ? "text-muted-foreground" : "text-amber-600 dark:text-amber-400")}>
              {step.holder ? (step.holder !== step.office ? `(${step.holder})` : "") : t("town.vacancy", { defaultValue: "vacancy: waits until someone moves in" })}
            </span>
            {ran && <span className="ml-1.5 text-xs text-muted-foreground">· {ran.status}{ran.note ? `: ${ran.note}` : ""}</span>}
          </span>
          <span className="mt-0.5 block text-xs text-muted-foreground">{step.duty}</span>
          {ran?.report && <details className="mt-1"><summary className="cursor-pointer text-xs text-muted-foreground">{t("town.roundReport", { defaultValue: "Its report" })}</summary>
            <p className="mt-1 whitespace-pre-wrap text-xs">{ran.report}</p></details>}
          <StepAccess step={step} busy={busy} onAccess={(access, commands) => onAccess(index + 1, access, commands)} />
        </div>;
      })}
      {round.rehearsal && <RehearsalReport rehearsal={round.rehearsal} busy={busy} onApprove={() => onApprove(round.rehearsal!.id)} />}
    </SettingsGroup>
  </section>;
}

/** What one step may do: its access beyond (or short of) the office's profile, and exact commands. Approving the round
 *  approves exactly this; it runs with it, without asking. The same as `babo rounds access`. */
function StepAccess({ step, busy, onAccess }: { step: TownRoundStep; busy: boolean; onAccess: (access: Record<string, string>, commands?: string[]) => void }) {
  const { t } = useTranslation();
  const saved = step.commands.join("\n");
  const [commands, setCommands] = useState(saved);
  useEffect(() => setCommands(saved), [saved]);  // only when what's saved changes, not on every refresh
  const summary = [...Object.entries(step.access).map(([k, v]) => `${k}: ${v}`), ...step.commands.map((c) => `may run \`${c}\``)].join(" · ");
  return <details className="mt-1">
    <summary className="cursor-pointer text-xs text-muted-foreground">
      {t("town.stepAccess", { defaultValue: "What it may do: {{what}}", what: summary || t("town.asProfile", { defaultValue: "as in the profile" }) })}
    </summary>
    <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2">
      <SessionAccessFields values={step.access} disabled={busy} onChange={(key, value) => onAccess({ [key]: value || "profile" })} />
    </div>
    <label className="mt-2 block text-xs text-muted-foreground">
      {t("town.stepCommands", { defaultValue: "Exact commands it may run, one per line" })}
      <textarea className="mt-1 block w-full rounded border bg-background px-2 py-1 font-mono text-xs text-foreground" rows={2}
        value={commands} disabled={busy} onChange={(event) => setCommands(event.target.value)} />
    </label>
    <Button size="sm" variant="ghost" className="mt-1" disabled={busy || commands === saved}
      onClick={() => onAccess({}, commands.split("\n").map((c) => c.trim()).filter(Boolean))}>{t("town.saveCommands", { defaultValue: "Save the commands" })}</Button>
  </details>;
}

/** The latest rehearsal: what each step asked for and what came of it, and the access Babo proposes. */
function RehearsalReport({ rehearsal, busy, onApprove }: { rehearsal: TownRehearsal; busy: boolean; onApprove: () => void }) {
  const { t } = useTranslation();
  const proposes = rehearsal.proposal.some((p) => Object.keys(p.access).length || p.commands.length);
  return <div className="settings-list-row py-3 text-[13px]" aria-label={t("town.rehearsalReport", { defaultValue: "Rehearsal report" })} role="region">
    <span className="block font-medium">
      {t("town.rehearsalHead", { defaultValue: "Rehearsal ({{how}}, {{when}}): {{status}}", how: rehearsal.live ? t("town.live", { defaultValue: "live" }) : t("town.unattended", { defaultValue: "unattended" }),
        when: rehearsal.started.slice(0, 16).replace("T", " "), status: rehearsal.status })}
    </span>
    {rehearsal.steps.map((step, index) => {
      const wanted = rehearsal.proposal[index];
      return <div key={index} className="mt-1.5 text-xs">
        <span className="font-medium">{index + 1}. {step.office}</span>{step.resident ? ` (${step.resident})` : ""}: {step.status}{step.note ? ` · ${step.note}` : ""}
        {step.stopped && <span className="block text-amber-600 dark:text-amber-400">{t("town.rehearsalStopped", { defaultValue: "Something it asked for was refused, so it may not have got to the end: approve this much and rehearse again to see the rest." })}</span>}
        {step.asked.map((a, i) => <span key={i} className="block text-muted-foreground">{t("town.rehearsalAsked", { defaultValue: "asked to {{action}}: {{what}} ({{decision}})", action: a.action, what: a.resources.join(" · "), decision: a.decision })}</span>)}
        {wanted && (Object.keys(wanted.access).length > 0 || wanted.commands.length > 0) && <span className="block">
          {t("town.rehearsalProposes", { defaultValue: "proposes: {{what}}", what: [...Object.entries(wanted.access).map(([k, v]) => `${k} ${v}`), ...wanted.commands.map((c) => `may run \`${c}\``)].join(", ") })}
        </span>}
        {wanted?.cannot.map((c) => <span key={c} className="block text-muted-foreground">{t("town.rehearsalCannot", { defaultValue: "can't be given to a round: {{what}}", what: c })}</span>)}
      </div>;
    })}
    {rehearsal.status !== "running" && proposes && !rehearsal.approved && <Button size="sm" className="mt-2" disabled={busy} onClick={onApprove}>
      {t("town.approveAccess", { defaultValue: "Approve this access" })}
    </Button>}
    {rehearsal.approved && <p className="mt-2 text-xs text-muted-foreground">{t("town.accessApproved", { defaultValue: "Approved {{when}}.", when: rehearsal.approved.slice(0, 16).replace("T", " ") })}</p>}
  </div>;
}
