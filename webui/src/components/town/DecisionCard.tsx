import { useState } from "react";
import { Check, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { TownDecisionKind, TownSessionRequest } from "@/lib/api";

export function DecisionCard({ kind, id, digest, heading, yes, no, yesDisabled = false, note = "", reasonLabel, onDecide, children }: {
  kind: TownDecisionKind;
  id: string;
  /** For a session request: the fingerprint of what is shown, so the approval counts only for that. */
  digest?: string;
  heading: string;
  yes: string;
  no: string | null;
  yesDisabled?: boolean;
  note?: string;
  reasonLabel?: string;
  onDecide: (kind: TownDecisionKind, id: string, yes: boolean, reason: string, digest?: string) => Promise<void>;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (answer: boolean) => {
    setBusy(true);
    try { await onDecide(kind, id, answer, reason, digest); }
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

/** A session request word for word: the task, why, the access asked for against the resident's profile, the exact commands. */
export function SessionRequestDetails({ request }: { request: TownSessionRequest }) {
  const { t } = useTranslation();
  return <>
    <p className="whitespace-pre-wrap text-[13px]">{request.task}</p>
    <p className="mt-2 text-xs text-muted-foreground">{t("town.why", { defaultValue: "Why:" })} {request.reason}</p>
    <ul className="mt-2 space-y-0.5 text-xs">
      {Object.entries(request.access).map(([key, level]) => <li key={key}>
        {request.access_labels[key] ?? key}: <strong>{level}</strong>
        <span className="text-muted-foreground"> ({t("town.inProfile", { defaultValue: "profile: {{level}}", level: request.profile[key] ?? "deny" })})</span>
      </li>)}
      {Object.keys(request.access).length === 0 && <li className="text-muted-foreground">{t("town.accessAsProfile", { defaultValue: "Access as in the resident's profile." })}</li>}
    </ul>
    {request.commands.length > 0 && <div className="mt-2">
      <p className="text-xs text-muted-foreground">{t("town.exactCommands", { defaultValue: "May run exactly these commands, and no others:" })}</p>
      <pre className="mt-1 whitespace-pre-wrap rounded bg-muted px-2 py-1 text-xs">{request.commands.join("\n")}</pre>
    </div>}
    {request.wider.length > 0 && <p role="note" className="mt-2 text-xs text-amber-600 dark:text-amber-400">
      {t("town.beyondProfile", { defaultValue: "Beyond its profile: {{what}}", what: request.wider.join("; ") })}
    </p>}
  </>;
}
