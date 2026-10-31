import { useCallback, useEffect, useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { useTranslation } from "react-i18next";
import { DecisionCard, SessionRequestDetails } from "@/components/town/DecisionCard";
import { useClient } from "@/providers/ClientProvider";
import { decideTownItem, fetchPendingSessionRequests, type TownDecisionKind, type TownSessionRequest } from "@/lib/api";

/** How often an open chat checks for session requests waiting for Squid. */
export const SESSION_REQUEST_POLL_MS = 5_000;

/** Shown above the composer while a session request waits for Squid, so he can decide it right in the chat
 *  instead of checking `babo review`. The same decision, with the same fingerprint, as Town › Review. */
export function SessionRequestPrompt({ sessionKey }: { sessionKey: string }) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [pending, setPending] = useState<TownSessionRequest[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try { setPending((await fetchPendingSessionRequests(getToken())).pending); }
    catch { setPending([]); } // not a town, or the gateway is restarting: nothing to ask about
  }, [getToken]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, SESSION_REQUEST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const decide = async (kind: TownDecisionKind, id: string, yes: boolean, reason: string, digest?: string) => {
    setError("");
    try { await decideTownItem(client, kind, id, yes, reason, digest); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    await load();
  };

  if (pending.length === 0) return null;
  const ordered = [...pending.filter((r) => r.session_key === sessionKey), ...pending.filter((r) => r.session_key !== sessionKey)];
  return <section role="alert" aria-label={t("sessionPrompt.label", { defaultValue: "Waiting for your approval" })}
    className="mx-2 mb-2 max-h-[50vh] overflow-y-auto rounded-xl border border-amber-500/50 bg-amber-500/5 px-4 py-2">
    <p className="flex items-center gap-1.5 pt-1 text-xs font-medium text-amber-700 dark:text-amber-300">
      <ShieldQuestion className="h-3.5 w-3.5" aria-hidden />
      {t("sessionPrompt.label", { defaultValue: "Waiting for your approval" })}
    </p>
    {ordered.map((r) => <DecisionCard key={r.id} kind="session" id={r.id} digest={r.digest} onDecide={decide}
      heading={r.session_key === sessionKey
        ? t("sessionPrompt.here", { defaultValue: "{{who}} asks, in this chat, for a session of {{resident}}", who: r.requested_by, resident: r.resident })
        : t("sessionPrompt.elsewhere", { defaultValue: "{{who}} asks, from {{chat}}, for a session of {{resident}}", who: r.requested_by, chat: r.session_key || "another chat", resident: r.resident })}
      yes={t("town.approveRun", { defaultValue: "Approve and run" })} no={t("town.decline", { defaultValue: "Decline" })}
      note={t("town.requestRuns", { defaultValue: "It runs on your machine with nobody at a terminal: anything not allowed here is denied. The result goes to {{who}}.", who: r.requested_by })}>
      <SessionRequestDetails request={r} />
    </DecisionCard>)}
    {error && <p role="alert" className="pb-2 text-xs text-destructive">{error}</p>}
  </section>;
}
