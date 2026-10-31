import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, ChevronLeft, Loader2, Stethoscope } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { MarkdownText } from "@/components/MarkdownText";
import { DecisionCard, SessionRequestDetails } from "@/components/town/DecisionCard";
import { useClient } from "@/providers/ClientProvider";
import {
  decideTownItem,
  fetchTown,
  fetchTownReview,
  changeTownSession,
  fetchTownProfiles,
  fetchTownSessions,
  fetchTownSettings,
  updateTownProfile,
  updateTownSettings,
  leaveTownNote,
  nameTown,
  requestTownUpkeep,
  runTownDoctor,
  type TownChange,
  type TownDecisionKind,
  type TownDoctorReport,
  type TownLevel,
  type TownOverview,
  type TownProfile,
  type TownReview,
  type TownSession,
  type TownSessions,
  type TownSetting,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import "./town.css";

type TownTab = "town" | "sessions" | "review" | "profiles" | "doctor" | "settings";

function errorText(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : String(reason);
}

function when(value: string): string {
  if (!value) return "never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** The town: who lives here, what waits for Squid, what each agent may do, and whether everything works.
 *  The same data and decisions as `babo town`, `babo review`, `babo note`, `babo profile` and `babo doctor`. */
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
              { value: "sessions", label: t("town.tab.sessions", { defaultValue: "Sessions" }) },
              { value: "review", label: waiting ? t("town.tab.reviewWaiting", { defaultValue: "Review ({{count}})", count: waiting }) : t("town.tab.review", { defaultValue: "Review" }) },
              { value: "profiles", label: t("town.tab.profiles", { defaultValue: "Profiles" }) },
              { value: "doctor", label: t("town.tab.doctor", { defaultValue: "Doctor" }) },
              { value: "settings", label: t("town.tab.settings", { defaultValue: "Settings" }) },
            ]} />
          {notice && <p role="status" className="settings-list-inset flex items-center gap-2 text-[13px] text-foreground"><Check className="h-4 w-4 shrink-0" />{notice}</p>}
          {error && <p role="alert" className="settings-list-inset flex items-center gap-2 text-[13px] text-destructive"><AlertCircle className="h-4 w-4 shrink-0" />{error}</p>}
          {tab === "town" && <ResidentsView town={town} onChanged={changed} onError={setError} transport={client} />}
          {tab === "review" && <ReviewView token={getToken} transport={client} onChanged={changed} onError={setError} />}
          {tab === "sessions" && <SessionsView token={getToken} transport={client} onNotice={(message) => { setNotice(message); setError(""); }} onError={setError} />}
          {tab === "profiles" && <ProfilesView token={getToken} transport={client} onNotice={(message) => { setNotice(message); setError(""); }} onError={setError} />}
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

    <UpkeepView town={town} transport={transport} onChanged={onChanged} onError={onError} />

    <TownNameForm current={town.name} transport={transport} onChanged={onChanged} onError={onError} />
  </>;
}

function UpkeepView({ town, transport, onChanged, onError }: {
  town: TownOverview;
  transport: Transport;
  onChanged: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const up = town.upkeep;
  const catchUp = async () => {
    setBusy(true);
    try { onChanged(await requestTownUpkeep(transport)); }
    catch (reason) { onError(errorText(reason)); }
    finally { setBusy(false); }
  };
  const state = up.service !== "running"
    ? t("town.upkeepStopped", { defaultValue: "The upkeep service isn't running, so nothing is reviewed or curated. On the host: systemctl --user start babo-upkeep" })
    : up.running ? t("town.upkeepWorking", { defaultValue: "Working now." })
      : up.waiting.length === 0 ? t("town.upkeepIdle", { defaultValue: "Nothing is waiting." })
        : up.waiting_for ? t("town.upkeepWaiting", { defaultValue: "Waiting, because {{why}}.", why: up.waiting_for })
          : t("town.upkeepStarting", { defaultValue: "Starting shortly." });
  return <>
    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{t("town.upkeep", { defaultValue: "Upkeep" })}</h2>
    <SettingsGroup>
      <div className="settings-list-row flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
        <span className="min-w-[14rem] flex-1">
          <span className="block text-[13px]">{state}</span>
          {up.waiting.length > 0 && <span className="mt-1 block text-xs text-muted-foreground">{up.waiting.join(" · ")}</span>}
          <span className="mt-1 block text-xs text-muted-foreground">
            {t("town.upkeepHow", { defaultValue: "Memory reviews and curation wait until nobody has used the model for {{minutes}} minutes; everything waiting is done in quiet hours ({{hours}}).", minutes: up.wait_minutes, hours: up.quiet_hours })}
          </span>
        </span>
        <Button variant="secondary" disabled={busy || up.catch_up_requested || up.running || up.service !== "running"} onClick={() => void catchUp()}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : up.catch_up_requested ? t("town.catchUpAsked", { defaultValue: "Catch-up requested" }) : t("town.catchUp", { defaultValue: "Catch up now" })}
        </Button>
      </div>
      {town.last_upkeep
        ? <details className="settings-list-row py-3">
          <summary className="cursor-pointer text-[13px]">
            {t("town.lastUpkeep", { defaultValue: "Last upkeep" })}: {town.last_upkeep.pass} · {town.last_upkeep.finished ? t("town.upkeepDone", { defaultValue: "finished" }) : t("town.upkeepUnfinished", { defaultValue: "not finished (it resumes next time)" })}
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{town.last_upkeep.log}</pre>
        </details>
        : <p className="settings-list-row py-3 text-[13px] text-muted-foreground">{t("town.noUpkeep", { defaultValue: "No upkeep has run yet." })}</p>}
    </SettingsGroup>
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

  const decide = async (kind: TownDecisionKind, id: string, yes: boolean, reason: string, digest?: string) => {
    try { onChanged(await decideTownItem(transport, kind, id, yes, reason, digest)); await load(); }
    catch (reason_) { onError(errorText(reason_)); }
  };

  if (!review) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;
  const empty = !review.sessions.length && !review.tools.length && !review.proposals.length && !review.founding.length && !review.flags.length;
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.reviewIntro", { defaultValue: "Nothing changes unless you decide. Whatever reason you give goes to the resident concerned. This is not an audit of anyone's memory." })}
    </p>
    {empty && <p className="settings-list-inset text-[13px]">{t("town.reviewEmpty", { defaultValue: "Nothing is waiting for you." })}</p>}
    {review.sessions.length > 0 && <ReviewSection title={t("town.review.sessions", { defaultValue: "Session requests" })}>
      {review.sessions.map((r) => <DecisionCard key={r.id} kind="session" id={r.id} digest={r.digest} onDecide={decide}
        heading={t("town.sessionRequest", { defaultValue: "{{who}} asks for a session of {{resident}}", who: r.requested_by, resident: r.resident })}
        yes={t("town.approveRun", { defaultValue: "Approve and run" })} no={t("town.decline", { defaultValue: "Decline" })}
        note={t("town.requestRuns", { defaultValue: "It runs on your machine with nobody at a terminal: anything not allowed here is denied. The result goes to {{who}}.", who: r.requested_by })}>
        <SessionRequestDetails request={r} />
      </DecisionCard>)}
    </ReviewSection>}
    {review.recent.length > 0 && <ReviewSection title={t("town.review.recent", { defaultValue: "Recently run" })}>
      {review.recent.map((r) => <div key={r.id} className="settings-list-row py-3">
        <p className="text-[13px]"><span className="font-medium">{r.resident}</span>: {r.task}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {r.status} · {r.decided_by === "allow-all"
            ? t("town.byAllowAll", { defaultValue: "approved automatically (allow all)" })
            : r.status === "declined" ? t("town.declinedByYou", { defaultValue: "declined by you" }) : t("town.byYou", { defaultValue: "approved by you" })}
          {r.decided ? ` · ${when(r.decided)}` : ""}
          {r.commands.length ? ` · ${r.commands.join("; ")}` : ""}
        </p>
        {r.result && <p className="mt-1 whitespace-pre-wrap text-xs">{r.result}</p>}
      </div>)}
    </ReviewSection>}
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
        <Input className="w-40" inputMode={s.kind === "int" ? "numeric" : undefined} placeholder={s.kind === "hours" ? "HH:MM-HH:MM" : undefined}
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

function ProfilesView({ token, transport, onNotice, onError }: {
  token: () => string;
  transport: Parameters<typeof updateTownProfile>[0];
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<TownProfile[] | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => {
    fetchTownProfiles(token()).then((result) => setProfiles(result.profiles)).catch((reason) => onError(errorText(reason)));
  }, [onError, token]);
  if (!profiles) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;

  const change = async (agent: string, changes: Record<string, string | string[]>) => {
    setBusy(agent);
    try {
      const result = await updateTownProfile(transport, agent, changes);
      setProfiles(result.profiles); onNotice(result.message);
    } catch (reason) { onError(errorText(reason)); }
    finally { setBusy(""); }
  };
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.profilesIntro", { defaultValue: "What each agent may do. Keys and providers are set once in Settings; a profile decides which agents may use them. The same profiles are available with `babo profile`." })}
    </p>
    {profiles.map((profile) => <ProfileCard key={profile.agent} profile={profile} busy={busy === profile.agent}
      onChange={(changes) => void change(profile.agent, changes)} />)}
  </>;
}

function ProfileCard({ profile, busy, onChange }: {
  profile: TownProfile;
  busy: boolean;
  onChange: (changes: Record<string, string | string[]>) => void;
}) {
  const { t } = useTranslation();
  const template = profile.agent === "_new-residents";
  const [model, setModel] = useState(profile.model);
  const [memory, setMemory] = useState<Record<string, string>>({});
  useEffect(() => { setModel(profile.model); setMemory({}); }, [profile]);
  const memoryChanges = Object.fromEntries(Object.entries(memory).filter(([key, value]) =>
    value !== String(profile.memory[key as keyof TownProfile["memory"]] ?? "")).map(([key, value]) => [`memory.${key}`, value || "default"]));
  const textChanges: Record<string, string> = { ...memoryChanges, ...(!profile.main && model !== profile.model ? { model } : {}) };

  const title = profile.main
    ? t("town.profileMayor", { defaultValue: "Mayor" })
    : template ? t("town.profileNew", { defaultValue: "New residents" }) : `${profile.agent} · ${profile.office}`;
  const subtitle = profile.main
    ? t("town.profileMayorHelp", { defaultValue: "Babo's main agent. The same profile applies to every Mayor session, in the web interface, the terminal and chat apps. The Mayor uses Babo's model, set in Settings › Models." })
    : template ? t("town.profileNewHelp", { defaultValue: "What a newly founded resident starts with. Changing it doesn't change residents who already live here." })
      : t("town.profileResidentHelp", { defaultValue: "A resident. Its sessions start from this profile." });
  return <section aria-label={title} className="mt-2">
    <h2 className="settings-list-inset flex items-center gap-2 text-[13px] font-medium text-foreground">
      {title}
      {profile.main && <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">{t("town.mainAgent", { defaultValue: "Main agent" })}</span>}
      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
    </h2>
    <p className="settings-list-inset mb-1 text-xs text-muted-foreground">{subtitle}</p>
    <SettingsGroup>
      {profile.capabilities.map((cap) => <div key={cap.key} className="settings-list-row flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
        <span className="min-w-[14rem] flex-1">
          <span className="block text-[14px] font-medium">{cap.label}</span>
          <span className="mt-0.5 block text-xs text-muted-foreground">{cap.help}</span>
        </span>
        <SegmentedControl<TownLevel> mode="radio" value={cap.level} ariaLabel={`${title}: ${cap.label}`}
          onChange={(level) => { if (!busy && level !== cap.level) onChange({ [cap.key]: level }); }}
          options={cap.levels.map((level) => ({ value: level, label: t(`town.level.${level}`, { defaultValue: { allow: "Allow", ask: "Ask", deny: "Deny" }[level] }) }))} />
      </div>)}
      <div className="settings-list-row py-3">
        <span className="block text-[14px] font-medium">{t("town.mcpServers", { defaultValue: "MCP servers" })}</span>
        {profile.mcp_known.length === 0
          ? <span className="mt-0.5 block text-xs text-muted-foreground">{t("town.mcpNone", { defaultValue: "No MCP servers are set up in Babo yet. Add them in Settings; then choose here who may use them." })}</span>
          : <span className="mt-2 flex flex-wrap gap-x-5 gap-y-2">{profile.mcp_known.map((name) => <label key={name} className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" disabled={busy} checked={profile.mcp.includes(name)}
              onChange={(event) => onChange({ mcp: event.target.checked ? [...profile.mcp, name] : profile.mcp.filter((n) => n !== name) })} />
            {name}
          </label>)}</span>}
      </div>
      <form className="settings-list-row flex flex-wrap items-end gap-x-4 gap-y-3 py-3" onSubmit={(event) => { event.preventDefault(); if (Object.keys(textChanges).length) onChange(textChanges); }}>
        {!profile.main && <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          {t("town.profileModel", { defaultValue: "Model (used in opencode)" })}
          <Input className="w-56" placeholder={t("town.babodefault", { defaultValue: "Babo default" })} value={model} onChange={(event) => setModel(event.target.value)} />
        </label>}
        {(["checkpoint_every_tool_calls", "idle_minutes"] as const).map((key) => <label key={key} className="flex flex-col gap-1 text-xs text-muted-foreground">
          {key === "idle_minutes" ? t("town.profileIdle", { defaultValue: "Checkpoint after … quiet minutes" }) : t("town.profileEvery", { defaultValue: "Checkpoint every … tool calls" })}
          <Input className="w-40" inputMode="numeric" placeholder={`${profile.memory_defaults[key]} (${t("town.babowide", { defaultValue: "Babo-wide" })})`}
            value={memory[key] ?? String(profile.memory[key] ?? "")} onChange={(event) => setMemory((current) => ({ ...current, [key]: event.target.value }))} />
        </label>)}
        <Button type="submit" variant="secondary" disabled={busy || Object.keys(textChanges).length === 0}>{t("town.save", { defaultValue: "Save" })}</Button>
      </form>
    </SettingsGroup>
  </section>;
}

const SESSION_CHANGES = [
  { key: "files", label: "Change files" },
  { key: "shell", label: "Run commands" },
  { key: "subagents", label: "Start crew" },
  { key: "web_fetch", label: "Read web pages" },
  { key: "web_search", label: "Search the web" },
] as const;

/** Residents other than the Mayor run in opencode, one session at a time or several.
 *  The same as `babo session list|new|open|freeze`. */
function SessionsView({ token, transport, onNotice, onError }: {
  token: () => string;
  transport: Parameters<typeof changeTownSession>[0];
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [data, setData] = useState<TownSessions | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => {
    fetchTownSessions(token()).then(setData).catch((reason) => onError(errorText(reason)));
  }, [onError, token]);
  if (!data) return <p className="settings-list-inset text-[13px] text-muted-foreground"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />{t("town.loading", { defaultValue: "Loading the town…" })}</p>;

  const act = async (key: string, action: "new" | "resume" | "freeze" | "remove", payload: Parameters<typeof changeTownSession>[2]) => {
    setBusy(key);
    try {
      const result = await changeTownSession(transport, action, payload);
      setData(result); onNotice(result.message);
    } catch (reason) { onError(errorText(reason)); }
    finally { setBusy(""); }
  };
  return <>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.sessionsIntro", { defaultValue: "Residents other than the Mayor work in opencode. Each session starts from the resident's profile; freezing one stops it, and resuming it continues exactly where it was; removing one drops it for good. In the terminal: `babo session open <resident>`." })}
    </p>
    {!data.opencode && <p role="note" className="settings-list-inset text-[13px] text-destructive">
      {t("town.noOpencode", { defaultValue: "opencode isn't installed where Babo runs, so residents' sessions can't start. Install it in the box with: curl -fsSL https://opencode.ai/install | bash" })}
    </p>}
    {data.residents.length === 0 && <p className="settings-list-inset text-[13px]">{t("town.noResidents", { defaultValue: "Only the Mayor lives here so far." })}</p>}
    {data.residents.map((r) => <section key={r.id} aria-label={r.name || r.id} className="mt-2">
      <h2 className="settings-list-inset text-[13px] font-medium text-foreground">{r.name ? `${r.name} (${r.id})` : r.id} · <span className="text-muted-foreground">{r.office}</span></h2>
      <SettingsGroup>
        {r.sessions.map((s) => <SessionRow key={s.id} session={s} busy={busy === s.id}
          onResume={() => void act(s.id, "resume", { resident: r.id, id: s.id })}
          onFreeze={() => void act(s.id, "freeze", { resident: r.id, id: s.id })}
          onRemove={() => void act(s.id, "remove", { resident: r.id, id: s.id })} />)}
        <NewSessionForm resident={r.id} disabled={!data.opencode || busy === `new:${r.id}`} busy={busy === `new:${r.id}`}
          onStart={(title, overrides) => void act(`new:${r.id}`, "new", { resident: r.id, title, overrides })} />
      </SettingsGroup>
    </section>)}
  </>;
}

function SessionRow({ session, busy, onResume, onFreeze, onRemove }: {
  session: TownSession;
  busy: boolean;
  onResume: () => void;
  onFreeze: () => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const [removing, setRemoving] = useState(false);
  const changes = Object.entries(session.overrides).map(([key, value]) => `${key}: ${value}`).join(", ");
  const running = session.status === "running";
  return <div className="settings-list-row flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
    <span className="min-w-[14rem] flex-1">
      <span className="block text-[14px] font-medium">{session.title}</span>
      <span className="mt-0.5 block text-xs text-muted-foreground">
        {running ? t("town.sessionRunning", { defaultValue: "running" }) : session.status === "frozen" ? t("town.sessionFrozen", { defaultValue: "frozen" }) : t("town.sessionNew", { defaultValue: "not started" })}
        {" · "}{t("town.lastActive", { defaultValue: "last active {{when}}", when: when(session.last_active) })}
        {changes ? ` · ${t("town.sessionChanges", { defaultValue: "this session: {{changes}}", changes })}` : ""}
      </span>
      {running && <span className="mt-0.5 block text-xs text-muted-foreground">
        {t("town.sessionLogin", { defaultValue: "opencode asks for a login: user opencode, password {{password}}", password: session.password })}
      </span>}
    </span>
    {running && <Button asChild variant="secondary" size="sm"><a href={session.url} target="_blank" rel="noreferrer">{t("town.openSession", { defaultValue: "Open" })}</a></Button>}
    {running
      ? <Button variant="ghost" size="sm" disabled={busy} onClick={onFreeze}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.freeze", { defaultValue: "Freeze" })}</Button>
      : <Button variant="ghost" size="sm" disabled={busy} onClick={onResume}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.resume", { defaultValue: "Resume" })}</Button>}
    {removing
      ? <span role="group" aria-label={t("town.removeSession", { defaultValue: "Remove" })} className="flex basis-full flex-wrap items-center justify-end gap-2 text-xs">
        <span className="text-muted-foreground">{t("town.removeWarning", { defaultValue: "Remove it for good? It stops without being reviewed into memory; what it did stays in the event log." })}</span>
        <Button variant="destructive" size="sm" disabled={busy} onClick={() => { setRemoving(false); onRemove(); }}>{t("town.removeConfirm", { defaultValue: "Remove for good" })}</Button>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => setRemoving(false)}>{t("town.keepSession", { defaultValue: "Keep it" })}</Button>
      </span>
      : <Button variant="ghost" size="sm" disabled={busy} className="text-muted-foreground" onClick={() => setRemoving(true)}>{t("town.removeSession", { defaultValue: "Remove" })}</Button>}
  </div>;
}

function NewSessionForm({ resident, disabled, busy, onStart }: {
  resident: string;
  disabled: boolean;
  busy: boolean;
  onStart: (title: string, overrides: Record<string, string>) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  return <form className="settings-list-row flex flex-col gap-3 py-3" onSubmit={(event) => { event.preventDefault(); onStart(title, overrides); setTitle(""); setOverrides({}); }}>
    <div className="flex flex-wrap gap-2">
      <Input className="min-w-[14rem] flex-1" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)}
        aria-label={t("town.sessionTitle", { defaultValue: "What the new session is for ({{who}})", who: resident })}
        placeholder={t("town.sessionTitlePlaceholder", { defaultValue: "What is this session for? (optional)" })} />
      <Button type="submit" disabled={disabled}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.newSession", { defaultValue: "New session" })}</Button>
    </div>
    <details>
      <summary className="cursor-pointer text-xs text-muted-foreground">{t("town.sessionOptions", { defaultValue: "Change what this session may do" })}</summary>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2">
        {SESSION_CHANGES.map((c) => <label key={c.key} className="flex items-center gap-2 text-[13px]">
          {t(`town.cap.${c.key}`, { defaultValue: c.label })}
          <select className="rounded border bg-background px-1 py-0.5 text-[13px]" value={overrides[c.key] ?? ""}
            onChange={(event) => setOverrides((current) => {
              const next = { ...current };
              if (event.target.value) next[c.key] = event.target.value; else delete next[c.key];
              return next;
            })}>
            <option value="">{t("town.asProfile", { defaultValue: "as in the profile" })}</option>
            <option value="allow">{t("town.level.allow", { defaultValue: "Allow" })}</option>
            <option value="ask">{t("town.level.ask", { defaultValue: "Ask" })}</option>
            <option value="deny">{t("town.level.deny", { defaultValue: "Deny" })}</option>
          </select>
        </label>)}
      </div>
    </details>
  </form>;
}
