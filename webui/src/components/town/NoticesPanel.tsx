import { useCallback, useEffect, useState } from "react";
import { BellRing } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownNotices, readTownNotices, type TownNotice } from "@/lib/api";

/** How often an open chat or the Town page checks for new notices. */
export const NOTICES_POLL_MS = 15_000;

/** New notices for the owner (a session wrapped up, a resident waiting for an answer), until they're read.
 *  The same as `babo notices`. Shown above the chat's message box and on the Sessions tab. */
export function NoticesPanel({ className = "" }: { className?: string }) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [notices, setNotices] = useState<TownNotice[]>([]);

  const load = useCallback(async () => {
    try { setNotices((await fetchTownNotices(getToken())).notices ?? []); }
    catch { setNotices([]); } // not a town, or Babo is restarting
  }, [getToken]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, NOTICES_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const read = async (ids?: string[]) => {
    try { setNotices((await readTownNotices(client, ids)).notices ?? []); } catch { /* shown again next time */ }
  };

  if (notices.length === 0) return null;
  return <section aria-label={t("notices.label", { defaultValue: "Notices" })}
    className={`mx-2 mb-2 rounded-xl border border-sky-500/40 bg-sky-500/5 px-4 py-2 ${className}`}>
    <div className="flex items-center justify-between gap-2 pt-1">
      <p className="flex items-center gap-1.5 text-xs font-medium text-sky-700 dark:text-sky-300">
        <BellRing className="h-3.5 w-3.5" aria-hidden />{t("notices.label", { defaultValue: "Notices" })}
      </p>
      <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => void read()}>{t("notices.readAll", { defaultValue: "Mark all read" })}</Button>
    </div>
    <ul className="space-y-1 py-1">
      {notices.map((n) => <li key={n.id} className="flex items-start justify-between gap-2 text-[13px]">
        <span><span className="text-muted-foreground">{new Date(n.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span> {n.text}</span>
        <button type="button" className="shrink-0 text-xs text-muted-foreground underline" onClick={() => void read([n.id])}>
          {t("notices.read", { defaultValue: "Read" })}
        </button>
      </li>)}
    </ul>
  </section>;
}
