import { useCallback, useEffect, useState } from "react";
import { Gauge, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownEval, runTownEval, type TownEvalStatus } from "@/lib/api";

const EVAL_POLL_MS = 3_000;

/** How well each model preset does the town's work: the same as `babo eval`. A run goes through the gateway,
 *  task by task, in the background; keep one as the preset's baseline and later runs compare with it. */
export function EvalPanel() {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [status, setStatus] = useState<TownEvalStatus | null>(null);
  const [preset, setPreset] = useState("");
  const [baseline, setBaseline] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try { setStatus(await fetchTownEval(getToken())); } catch { /* not a town */ }
  }, [getToken]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!status?.running) return;
    const timer = window.setInterval(() => void load(), EVAL_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, status?.running]);
  if (!status || !Array.isArray(status.presets)) return null;

  const chosen = preset || status.presets.find((p) => p !== "default") || status.presets[0] || "";
  const start = async () => {
    setError("");
    try { const next = await runTownEval(client, chosen, baseline); setStatus(next); setMessage(next.message); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <section aria-label={t("town.evalTitle", { defaultValue: "Model eval" })} className="mt-4">
    <h2 className="settings-list-inset text-[13px] font-medium text-foreground">{t("town.evalTitle", { defaultValue: "Model eval" })}</h2>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.evalIntro", { defaultValue: "{{count}} representative tasks (choosing tools, leaving them alone, following through, acting on the baton, writing memory), each checked by Babo, on a preset of your choice. Keep a run as the preset's baseline to compare later runs with. Same as `babo eval`.", count: status.tasks })}
    </p>
    <div className="settings-list-inset flex flex-wrap items-center gap-3">
      <select aria-label={t("town.evalPreset", { defaultValue: "Preset to evaluate" })} className="rounded border bg-background px-2 py-1.5 text-[13px]"
        value={chosen} onChange={(event) => setPreset(event.target.value)} disabled={!!status.running}>
        {status.presets.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
      <label className="flex items-center gap-2 text-[13px]">
        <input type="checkbox" checked={baseline} onChange={(event) => setBaseline(event.target.checked)} disabled={!!status.running} />
        {t("town.evalBaseline", { defaultValue: "Keep as its baseline" })}
      </label>
      <Button onClick={() => void start()} disabled={!!status.running || !chosen} className="gap-2">
        {status.running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Gauge className="h-4 w-4" />}
        {status.running
          ? t("town.evalRunning", { defaultValue: "Evaluating {{preset}}: {{done}}/{{total}}", ...status.running })
          : t("town.runEval", { defaultValue: "Run the eval" })}
      </Button>
    </div>
    {message && !status.running && <p role="status" className="settings-list-inset text-[13px] text-muted-foreground">{message}</p>}
    {error && <p role="alert" className="settings-list-inset text-[13px] text-destructive">{error}</p>}
    {status.latest.length > 0 && <SettingsGroup>
      {status.latest.map((row) => <div key={row.preset} className="settings-list-row flex flex-wrap items-center gap-x-3 py-3 text-[13px]">
        <span className="min-w-[10rem] flex-1 font-medium">{row.preset}{row.model ? <span className="font-normal text-muted-foreground"> · {row.model}</span> : null}</span>
        <span>{t("town.evalScore", { defaultValue: "{{passed}}/{{total}} passed", passed: row.passed, total: row.total })}</span>
        <span className="text-xs text-muted-foreground">
          {row.baseline
            ? t("town.evalAgainst", { defaultValue: "baseline {{passed}}/{{total}} ({{when}})", passed: row.baseline.passed, total: row.baseline.total, when: row.baseline.at.slice(0, 10) })
            : t("town.evalNoBaseline", { defaultValue: "no baseline yet" })}
          {" · "}{row.at.slice(0, 16).replace("T", " ")}
        </span>
      </div>)}
    </SettingsGroup>}
  </section>;
}
