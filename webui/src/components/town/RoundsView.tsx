import { useCallback, useEffect, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownRounds, runTownRound, setTownRound, type TownRound, type TownRounds } from "@/lib/api";
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
      onSet={(change) => void act(round.name, () => setTownRound(client, round.name, change))} />)}
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

function RoundCard({ round, busy, onRun, onSet }: {
  round: TownRound; busy: boolean; onRun: () => void; onSet: (change: { enabled?: boolean; runs?: "wait" | "now" }) => void;
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
        </div>;
      })}
    </SettingsGroup>
  </section>;
}
