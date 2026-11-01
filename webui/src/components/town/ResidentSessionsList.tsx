import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Pin } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownSessions, type TownSession } from "@/lib/api";
import { cn } from "@/lib/utils";

/** How often the sidebar checks on residents' sessions. */
export const RESIDENT_SESSIONS_POLL_MS = 15_000;
const COLLAPSED_KEY = "babo-webui.resident-sessions-collapsed.v1";

export interface ResidentSessionRef { resident: string; id: string }

type Row = TownSession & { residentName: string };

/** Residents' sessions in the sidebar, beside the Mayor's chats: one list of every session in the town.
 *  Pinned ones first, then the ones working or waiting for you, then the latest. A click opens it on
 *  the Town page's Sessions tab, with all its actions. Nothing shows when there's no town or no session. */
export function ResidentSessionsList({ onOpen, activeId }: {
  onOpen: (session: ResidentSessionRef) => void;
  activeId?: string | null;
}) {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [rows, setRows] = useState<Row[]>([]);
  const [collapsed, setCollapsed] = useState(() => {
    try { return window.localStorage.getItem(COLLAPSED_KEY) === "1"; } catch { return false; }
  });

  const load = useCallback(async () => {
    try {
      const data = await fetchTownSessions(getToken());
      setRows(data.residents.flatMap((r) => r.sessions.map((s) => ({ ...s, residentName: r.name || r.id }))));
    } catch { setRows([]); } // not a town, or Babo is restarting
  }, [getToken]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, RESIDENT_SESSIONS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const sorted = useMemo(() => {
    const busy = (s: Row) => s.live?.state === "waiting" || s.live?.state === "working" ? 1 : 0;
    return [...rows].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || busy(b) - busy(a)
      || (b.last_active || b.created).localeCompare(a.last_active || a.created));
  }, [rows]);

  const toggle = () => setCollapsed((value) => {
    try { window.localStorage.setItem(COLLAPSED_KEY, value ? "0" : "1"); } catch { /* only this browser forgets */ }
    return !value;
  });

  if (sorted.length === 0) return null;
  const label = t("sidebar.residentSessions", { defaultValue: "Residents' sessions" });
  return <section aria-label={label} className="shrink-0 px-2 pb-1">
    <button type="button" onClick={toggle} aria-expanded={!collapsed}
      className="flex w-full items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground hover:text-foreground">
      {collapsed ? <ChevronRight className="h-3.5 w-3.5" aria-hidden /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden />}
      {label}
      <span className="ml-auto font-normal">{sorted.length}</span>
    </button>
    {!collapsed && <ul className="max-h-56 overflow-y-auto">
      {sorted.map((s) => <li key={`${s.resident}/${s.id}`}>
        <button type="button" onClick={() => onOpen({ resident: s.resident, id: s.id })}
          aria-current={activeId === s.id ? "true" : undefined}
          className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-sidebar-accent",
            activeId === s.id && "bg-sidebar-accent")}>
          <StateDot state={s.live?.state ?? s.status} />
          <span className="min-w-0 flex-1">
            <span className="block truncate">{s.title}</span>
            <span className="block truncate text-[11px] text-muted-foreground">
              {s.residentName}{(s.tags ?? []).map((tag) => ` · #${tag}`).join("")}
            </span>
          </span>
          {s.pinned && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" aria-label={t("town.pinned", { defaultValue: "pinned" })} />}
        </button>
      </li>)}
    </ul>}
  </section>;
}

function StateDot({ state }: { state: string }) {
  const { t } = useTranslation();
  const color = state === "working" ? "bg-emerald-500" : state === "waiting" ? "bg-amber-500"
    : state === "idle" || state === "starting" || state === "running" ? "bg-sky-500" : "bg-muted-foreground/40";
  const name = {
    working: t("town.state.working", { defaultValue: "working" }), waiting: t("town.state.waiting", { defaultValue: "waiting for you" }),
    idle: t("town.state.idle", { defaultValue: "idle" }), starting: t("town.state.starting", { defaultValue: "starting" }),
    running: t("town.sessionRunning", { defaultValue: "running" }), frozen: t("town.sessionFrozen", { defaultValue: "frozen" }),
    new: t("town.sessionNew", { defaultValue: "not started" }),
  }[state] ?? state;
  return <span role="img" aria-label={name} title={name} className={cn("h-2 w-2 shrink-0 rounded-full", color)} />;
}
