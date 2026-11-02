import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Circle, CircleDot, MinusCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownTasks, type TownTaskList } from "@/lib/api";
import { cn } from "@/lib/utils";

const POLL_MS = 15_000;

const ICON = { todo: Circle, doing: CircleDot, done: CheckCircle2, dropped: MinusCircle } as const;

/** What the agents are working on: each task list with something still open (`babo tasks`). */
export function TasksPanel() {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [lists, setLists] = useState<TownTaskList[] | null>(null);
  const load = useCallback(async () => {
    try { setLists((await fetchTownTasks(getToken())).lists); } catch { setLists((current) => current ?? []); }
  }, [getToken]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);
  if (!lists || lists.length === 0) return null;
  return <section aria-label={t("town.workingOn", { defaultValue: "Working on" })}>
    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{t("town.workingOn", { defaultValue: "Working on" })}</h2>
    <SettingsGroup>
      {lists.map((list) => <div key={`${list.resident}/${list.session}`} className="settings-list-row py-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span className="text-[14px] font-medium">{list.resident}</span>
          <span className="text-xs text-muted-foreground">
            {t("town.tasksProgress", { defaultValue: "{{done}} of {{total}} done", done: list.done, total: list.total })} · {list.session}
          </span>
        </div>
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden>
          <div className="h-full rounded-full bg-emerald-500/70 transition-[width] duration-500" style={{ width: `${list.total ? (100 * list.done) / list.total : 0}%` }} />
        </div>
        <ul className="mt-2 space-y-1">
          {list.items.map((item, i) => {
            const Icon = ICON[item.status];
            return <li key={i} className={cn("flex items-start gap-2 text-[13px]", item.status === "done" && "text-muted-foreground line-through",
              item.status === "dropped" && "text-muted-foreground/70 line-through", item.status === "doing" && "font-medium")}>
              <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", item.status === "doing" ? "text-amber-500" : item.status === "done" ? "text-emerald-500" : "text-muted-foreground")}
                aria-label={t(`town.taskStatus.${item.status}`, { defaultValue: item.status })} />
              {item.task}
            </li>;
          })}
        </ul>
      </div>)}
    </SettingsGroup>
  </section>;
}
