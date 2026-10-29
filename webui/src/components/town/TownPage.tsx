import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, ChevronLeft, Loader2, Stethoscope, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { MarkdownText } from "@/components/MarkdownText";
import { useClient } from "@/providers/ClientProvider";
import {
  decideTownItem,
  fetchTown,
  fetchTownReview,
  fetchTownSettings,
  updateTownSettings,
  leaveTownNote,
  nameTown,
  runTownDoctor,
  type TownChange,
  type TownDecisionKind,
  type TownDoctorReport,
  type TownOverview,
  type TownReview,
  type TownSetting,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import "./town.css";

type TownTab = "town" | "review" | "doctor" | "settings";

function errorText(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : String(reason);
}

function when(value: string): string {
  if (!value) return "never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** The town: who lives here, what waits for Squid, and whether everything works.
 *  The same data and decisions as `babo town`, `babo review`, `babo note` and `babo doctor`. */
export function TownPage({ mainNavigationExpanded = false, hostChromeInset = false, onBackToChat }: {
  mainNavigationExpanded?: boolean;
  hostChromeInset?: boolean;
  onBackToChat: () => void;
}) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [tab, setTab] = useState<TownTab>("town");
  const [town, setTown] = useState<TownOverview | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  const loadTown = useCallback(async () => {
    try {
      const next = await fetchTown(getToken());
      if (mounted.current) { setTown(next); setError(""); }
    } catch (reason) {
      if (mounted.current) setError(errorText(reason));
    }
  }, [getToken]);
  useEffect(() => { void loadTown(); }, [loadTown]);

  const changed = useCallback((change: TownChange) => {
    if (!mounted.current) return;
    setTown(change.town);
    setNotice(change.message);
    setError("");
  }, []);

  const waiting = town ? Object.values(town.pending).reduce((sum, n) => sum + n, 0) : 0;
  const title = town?.name || t("town.title", { defaultValue: "Your town" });

  return <div tabIndex={-1} role="region" aria-label={title} className="flex min-h-0 flex-1 flex-col overflow-hidden bg-settings-canvas outline-none">
    <div className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
      <div data-settings-section="town" data-main-navigation-expanded={mainNavigationExpanded}
        className={cn("settings-grid settings-feature-page mx-auto w-full animate-in fade-in-0 slide-in-from-bottom-1 py-6 duration-200 ease-out motion-reduce:animate-none sm:py-8 lg:py-12",
          hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]")}>
        {/* Not .settings-feature-header: the town's name stays visible even with the sidebar open. */}
        <div className="mb-7">
          <Button variant="ghost" size="sm" className="touch-target mb-4 gap-1 lg:hidden" onClick={onBackToChat}>
            <ChevronLeft className="h-4 w-4" />{t("settings.backToChat")}
          </Button>
          <h1 className="text-[24px] font-normal leading-tight tracking-normal text-foreground sm:text-[28px]">{title}</h1>
          {town && !town.name && <p className="mt-1 text-[13px] text-muted-foreground">
            {t("town.unnamed", { defaultValue: "The town has no name yet. A resident can propose one; it will show up in Review." })}
          </p>}
        </div>
        <div className="settings-stack">
          <SegmentedControl<TownTab> mode="tabs" value={tab} onChange={setTab} ariaLabel={t("town.sections", { defaultValue: "Town sections" })}
            options={[
              { value: "town", label: t("town.tab.town", { defaultValue: "Residents" }) },
              { value: "review", label: waiting ? t("town.tab.reviewWaiting", { defaultValue: "Review ({{count}})", count: waiting }) : t("town.tab.review", { defaultValue: "Review" }) },
              { value: "doctor", label: t("town.tab.doctor", { defaultValue: "Doctor" }) },
              { value: "settings", label: t("town.tab.settings", { defaultValue: "Settings" }) },
            ]} />
          {notice && <p role="status" className="settings-list-inset flex items-center gap-2 text-[13px] text-foreground"><Check className="h-4 w-4 shrink-0" />{notice}</p>}
          {error && <p role="alert" className="settings-list-inset flex items-center gap-2 text-[13px] text-destructive"><AlertCircle className="h-4 w-4 shrink-0" />{error}</p>}
          {tab === "town" && <ResidentsView town={town} onChanged={changed} onError={setError} transport={client} />}
          {tab === "review" && <ReviewView token={getToken} transport={client} onChanged={changed} onError={setError} />}
          {tab === "doctor" && <DoctorView token={getToken} />}
          {tab === "settings" && <TownSettingsView token={getToken} transport={client} onNotice={(message) => { setNotice(message); setError(""); }} onError={setError} />}
        </div>
      </div>
    </div>
  </div>;
}

type Transport = Parameters<typeof leaveTownNote>[0];

function ResidentsView({ town, transport, onChanged, onError }: {
  town: TownOverview | null;
  transport: Transport;
  onChanged: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [noteFor, setNoteFor] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  if (!town) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;

  const send = async () => {
    if (!noteFor || !note.trim()) return;
    setBusy(true);
    try { onChanged(await leaveTownNote(transport, noteFor, note)); setNote(""); setNoteFor(""); }
    catch (reason) { onError(errorText(reason)); }
    finally { setBusy(false); }
  };

  return <>
    <SettingsGroup>
      {town.residents.map((r) => <div key={r.id} className="settings-list-row flex flex-wrap items-center gap-x-3 gap-y-1 py-3">
        <span className="min-w-0 flex-1">
          <span className="block text-[14px] font-medium">{r.name ? `${r.name} (${r.id})` : r.id}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">
            {r.office} · {t("town.lastActive", { defaultValue: "last active {{when}}", when: when(r.last_active) })}
            {r.unread ? ` · ${t("town.unread", { defaultValue: "{{count}} unread", count: r.unread })}` : ""}
            {r.flags ? ` · ${t("town.flags", { defaultValue: "{{count}} flag(s)", count: r.flags })}` : ""}
            {r.status !== "active" ? ` · ${r.status}` : ""}
          </span>
        </span>
        <Button variant="ghost" size="sm" onClick={() => setNoteFor(noteFor === r.id ? "" : r.id)}>
          {t("town.leaveNote", { defaultValue: "Leave a note" })}
        </Button>
        {noteFor === r.id && <form className="flex w-full gap-2" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <Input autoFocus value={note} maxLength={4000} onChange={(event) => setNote(event.target.value)}
            aria-label={t("town.noteLabel", { defaultValue: "Note for {{who}}", who: r.name || r.id })}
            placeholder={t("town.notePlaceholder", { defaultValue: "They will see it at the start of their next session" })} />
          <Button type="submit" disabled={busy || !note.trim()}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.send", { defaultValue: "Send" })}</Button>
        </form>}
      </div>)}
    </SettingsGroup>

    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{t("town.board", { defaultValue: "Notice board" })}</h2>
    <SettingsGroup>
      {town.board.length === 0
        ? <p className="settings-list-row py-3 text-[13px] text-muted-foreground">{t("town.boardEmpty", { defaultValue: "Nothing open on the board." })}</p>
        : town.board.map((post, index) => <div key={index} className="settings-list-row py-3">
          <span className="block text-[13px]">{post.text}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">{post.kind} · {post.author}</span>
        </div>)}
    </SettingsGroup>

    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{t("town.lastNight", { defaultValue: "Last night" })}</h2>
    <SettingsGroup>
      {town.last_night
        ? <details className="settings-list-row py-3">
          <summary className="cursor-pointer text-[13px]">
            {town.last_night.night} · {town.last_night.finished ? t("town.nightDone", { defaultValue: "finished" }) : t("town.nightUnfinished", { defaultValue: "not finished (it resumes next time)" })}
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{town.last_night.log}</pre>
        </details>
        : <p className="settings-list-row py-3 text-[13px] text-muted-foreground">{t("town.noNight", { defaultValue: "No night has run yet." })}</p>}
    </SettingsGroup>

    <TownNameForm current={town.name} transport={transport} onChanged={onChanged} onError={onError} />
  </>;
}

function TownNameForm({ current, transport, onChanged, onError }: {
  current: string;
  transport: Transport;
  onChanged: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(current);
  const [busy, setBusy] = useState(false);
  if (!open) {
    return <p className="settings-list-inset text-xs text-muted-foreground">
      {t("town.nameHint", { defaultValue: "The residents usually choose the town's name. You can also" })}{" "}
      <button type="button" className="underline underline-offset-2" onClick={() => setOpen(true)}>{t("town.nameItYourself", { defaultValue: "name it yourself" })}</button>.
    </p>;
  }
  const save = async () => {
    setBusy(true);
    try { onChanged(await nameTown(transport, name)); setOpen(false); }
    catch (reason) { onError(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <form className="settings-list-inset flex gap-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <Input autoFocus value={name} maxLength={60} onChange={(event) => setName(event.target.value)} aria-label={t("town.nameLabel", { defaultValue: "Town name" })} />
    <Button type="submit" disabled={busy || !name.trim()}>{t("town.saveName", { defaultValue: "Save" })}</Button>
    <Button type="button" variant="ghost" onClick={() => setOpen(false)}>{t("town.cancel", { defaultValue: "Cancel" })}</Button>
  </form>;
}

function ReviewView({ token, transport, onChanged, onError }: {
  token: () => string;
  transport: Transport;
  onChanged: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [review, setReview] = useState<TownReview | null>(null);
  const load = useCallback(async () => {
    try { setReview(await fetchTownReview(token())); }
    catch (reason) { onError(errorText(reason)); }
  }, [onError, token]);
  useEffect(() => { void load(); }, [load]);

  const decide = async (kind: TownDecisionKind, id: string, yes: boolean, reason: string) => {
    try { onChanged(await decideTownItem(transport, kind, id, yes, reason)); await load(); }
    catch (reason_) { onError(errorText(reason_)); }
  };

  if (!review) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;
  const empty = !review.tools.length && !review.proposals.length && !review.founding.length && !review.flags.length;
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.reviewIntro", { defaultValue: "Nothing changes unless you decide. Whatever reason you give goes to the resident concerned. This is not an audit of anyone's memory." })}
    </p>
    {empty && <p className="settings-list-inset text-[13px]">{t("town.reviewEmpty", { defaultValue: "Nothing is waiting for you." })}</p>}
    {review.tools.length > 0 && <ReviewSection title={t("town.review.tools", { defaultValue: "Proposed tools" })}>
      {review.tools.map((tool) => <DecisionCard key={tool.id} kind="tool" id={tool.id} onDecide={decide}
        heading={`${tool.name} — ${tool.author}`} yes={t("town.approve", { defaultValue: "Approve" })} no={t("town.reject", { defaultValue: "Reject" })}
        yesDisabled={tool.tested !== true}
        note={t("town.toolRuns", { defaultValue: "An approved tool runs on your machine for every resident." })}>
        <p className="text-[13px]">{tool.description}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {tool.tested === true ? t("town.testPassed", { defaultValue: "Passed its isolated test" }) : tool.tested === false ? t("town.testFailed", { defaultValue: "Failed its isolated test — it can't be approved" }) : t("town.testPending", { defaultValue: "Not tested yet (tested at night)" })}
          {tool.isolation ? ` (${tool.isolation})` : ""}
        </p>
        <Expandable label={t("town.inspectorReview", { defaultValue: "Inspector's review" })} text={tool.review || t("town.noReview", { defaultValue: "(none yet)" })} />
        <Expandable label={t("town.code", { defaultValue: "Code" })} text={tool.code} code />
        <Expandable label={t("town.test", { defaultValue: "Test" })} text={tool.test} code />
      </DecisionCard>)}
    </ReviewSection>}
    {review.proposals.length > 0 && <ReviewSection title={t("town.review.proposals", { defaultValue: "Proposals" })}>
      {review.proposals.map((p) => <DecisionCard key={p.id} kind="proposal" id={p.id} onDecide={decide}
        heading={`${p.title} — ${p.author}`} yes={t("town.accept", { defaultValue: "Accept" })} no={t("town.decline", { defaultValue: "Decline" })}
        note={p.town_name ? t("town.namesTown", { defaultValue: "Accepting names the town “{{name}}”.", name: p.town_name })
          : p.project ? t("town.movesProject", { defaultValue: "Accepting moves the project “{{name}}” to the commons.", name: p.project }) : ""}>
        <Expandable label={t("town.proposal", { defaultValue: "The proposal" })} text={p.body} open />
        <Expandable label={t("town.inspectorReview", { defaultValue: "Inspector's review" })} text={p.review || t("town.noReview", { defaultValue: "(none yet)" })} />
      </DecisionCard>)}
    </ReviewSection>}
    {review.founding.length > 0 && <ReviewSection title={t("town.review.founding", { defaultValue: "Founding requests" })}>
      {review.founding.map((f) => <DecisionCard key={f.id} kind="founding" id={f.id} onDecide={decide}
        heading={t("town.newOffice", { defaultValue: "New office “{{office}}” — asked by {{who}}", office: f.office, who: f.author })}
        yes={t("town.found", { defaultValue: "Found this resident" })} no={t("town.decline", { defaultValue: "Decline" })}
        note={t("town.foundingCost", { defaultValue: "A new resident uses compute on your machine every day it is active." })}>
        <Expandable label={t("town.request", { defaultValue: "The request" })} text={f.body} open />
      </DecisionCard>)}
    </ReviewSection>}
    {review.flags.length > 0 && <ReviewSection title={t("town.review.flags", { defaultValue: "Flags open for over a week" })}>
      {review.flags.map((f) => <DecisionCard key={f.id} kind="flag" id={f.id} onDecide={decide}
        heading={t("town.flagHeading", { defaultValue: "To {{to}} from {{by}}", to: f.to, by: f.by })}
        yes={t("town.resolve", { defaultValue: "Mark resolved" })} no={null} reasonLabel={t("town.noteOptional", { defaultValue: "Note (optional)" })}>
        <p className="text-[13px]">{f.claim}</p>
        <p className="mt-1 text-xs text-muted-foreground">{when(f.at)}</p>
      </DecisionCard>)}
    </ReviewSection>}
  </>;
}

function ReviewSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <>
    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{title}</h2>
    <SettingsGroup>{children}</SettingsGroup>
  </>;
}

function Expandable({ label, text, code = false, open = false }: { label: string; text: string; code?: boolean; open?: boolean }) {
  if (!text) return null;
  return <details className="mt-2" open={open}>
    <summary className="cursor-pointer text-xs text-muted-foreground">{label}</summary>
    {code
      ? <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2 font-mono text-xs">{text}</pre>
      : <div className="town-markdown mt-1 max-h-96 overflow-auto"><MarkdownText>{text}</MarkdownText></div>}
  </details>;
}

function DecisionCard({ kind, id, heading, yes, no, yesDisabled = false, note = "", reasonLabel, onDecide, children }: {
  kind: TownDecisionKind;
  id: string;
  heading: string;
  yes: string;
  no: string | null;
  yesDisabled?: boolean;
  note?: string;
  reasonLabel?: string;
  onDecide: (kind: TownDecisionKind, id: string, yes: boolean, reason: string) => Promise<void>;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (answer: boolean) => {
    setBusy(true);
    try { await onDecide(kind, id, answer, reason); }
    finally { setBusy(false); }
  };
  return <div className="settings-list-row py-4">
    <p className="text-[14px] font-medium">{heading}</p>
    <div className="mt-1">{children}</div>
    {note && <p className="mt-2 text-xs text-muted-foreground">{note}</p>}
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <Input value={reason} maxLength={4000} onChange={(event) => setReason(event.target.value)} className="min-w-[12rem] flex-1"
        aria-label={reasonLabel ?? t("town.reason", { defaultValue: "Your reason (goes to the resident)" })}
        placeholder={reasonLabel ?? t("town.reason", { defaultValue: "Your reason (goes to the resident)" })} />
      <Button disabled={busy || yesDisabled} onClick={() => void act(true)} className="gap-1"><Check className="h-4 w-4" />{yes}</Button>
      {no && <Button variant="outline" disabled={busy} onClick={() => void act(false)} className="gap-1"><X className="h-4 w-4" />{no}</Button>}
    </div>
  </div>;
}

function DoctorView({ token }: { token: () => string }) {
  const { t } = useTranslation();
  const [report, setReport] = useState<TownDoctorReport | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const run = async () => {
    setRunning(true); setError("");
    try { setReport(await runTownDoctor(token())); }
    catch (reason) { setError(errorText(reason)); }
    finally { setRunning(false); }
  };
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.doctorIntro", { defaultValue: "Checks every part of the town: the model, the queue, tool calling, memory, search, the residents and git. It never writes into a resident's memory. Same as `babo doctor`." })}
    </p>
    <div className="settings-list-inset">
      <Button onClick={() => void run()} disabled={running} className="gap-2">
        {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Stethoscope className="h-4 w-4" />}
        {running ? t("town.doctorRunning", { defaultValue: "Checking… (this talks to the model and can take a minute)" }) : t("town.runDoctor", { defaultValue: "Run the doctor" })}
      </Button>
    </div>
    {error && <p role="alert" className="settings-list-inset text-[13px] text-destructive">{error}</p>}
    {report && <SettingsGroup>
      {report.checks.map((check) => <div key={check.name} className="settings-list-row flex items-start gap-3 py-3">
        <span className={cn("w-12 shrink-0 text-xs font-semibold", check.status === "PASS" ? "text-emerald-600" : check.status === "WARN" ? "text-amber-600" : "text-destructive")}>{check.status}</span>
        <span className="min-w-0 flex-1"><span className="block text-[13px] font-medium">{check.name}</span>
          <span className="mt-0.5 block break-words text-xs text-muted-foreground">{check.detail}</span></span>
      </div>)}
    </SettingsGroup>}
  </>;
}

function TownSettingsView({ token, transport, onNotice, onError }: {
  token: () => string;
  transport: Parameters<typeof updateTownSettings>[0];
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<TownSetting[] | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    fetchTownSettings(token()).then((result) => setSettings(result.settings)).catch((reason) => onError(errorText(reason)));
  }, [onError, token]);
  if (!settings) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;

  const changes = Object.fromEntries(Object.entries(draft).filter(([key, value]) =>
    String(settings.find((s) => s.key === key)?.value ?? "") !== value));
  const save = async () => {
    setBusy(true);
    try {
      const result = await updateTownSettings(transport, changes);
      setSettings(result.settings); setDraft({}); onNotice(result.message);
    } catch (reason) { onError(errorText(reason)); }
    finally { setBusy(false); }
  };
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.settingsIntro", { defaultValue: "The town's own settings. The same list is available with `babo settings`. Your config file is backed up before each change." })}
    </p>
    <SettingsGroup>
      {settings.map((s) => <label key={s.key} className="settings-list-row flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
        <span className="min-w-[14rem] flex-1">
          <span className="block text-[14px] font-medium">{s.label}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">{s.help}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">{t("town.applies", { defaultValue: "Takes effect {{when}}.", when: s.applies })}</span>
        </span>
        <Input className="w-40" inputMode={s.kind === "int" ? "numeric" : undefined} placeholder={s.kind === "time" ? "HH:MM" : undefined}
          value={draft[s.key] ?? String(s.value)} onChange={(event) => setDraft((current) => ({ ...current, [s.key]: event.target.value }))} />
      </label>)}
    </SettingsGroup>
    <div className="settings-list-inset">
      <Button disabled={busy || Object.keys(changes).length === 0} onClick={() => void save()}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.saveSettings", { defaultValue: "Save settings" })}
      </Button>
    </div>
  </>;
}
