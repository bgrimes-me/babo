import { useCallback, useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownAllowAll, setTownAllowAll } from "@/lib/api";

type Duration = "1" | "8" | "off";

/** "Allow all" for one of the Mayor's chats: his session requests from it run without review.
 *  The same as `babo allow-all <chat>`. Off by default; turning it on asks first and says what it means. */
export function AllowAllControl({ sessionKey }: { sessionKey: string }) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [until, setUntil] = useState<string | null>(null); // null: off; "": until turned off
  const [asking, setAsking] = useState(false);
  const [duration, setDuration] = useState<Duration>("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const status = await fetchTownAllowAll(getToken());
      setUntil(status.active[sessionKey] ? status.active[sessionKey].until : null);
    } catch { setUntil(null); }
  }, [getToken, sessionKey]);
  useEffect(() => { void load(); }, [load]);

  const change = async (on: boolean) => {
    setBusy(true); setError("");
    try {
      const status = await setTownAllowAll(client, sessionKey, on, on && duration !== "off" ? Number(duration) : null);
      setUntil(status.active[sessionKey] ? status.active[sessionKey].until : null);
      setAsking(false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  const on = until !== null;
  return <>
    {on
      ? <span role="status" className="pointer-events-auto flex items-center gap-1.5 rounded-full bg-amber-500/15 px-2.5 py-1 text-xs font-medium text-amber-700 dark:text-amber-300">
        <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
        {until ? t("allowAll.onUntil", { defaultValue: "Allow all · until {{time}}", time: new Date(until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) })
          : t("allowAll.on", { defaultValue: "Allow all is on" })}
        <button type="button" className="ml-1 underline" disabled={busy} onClick={() => void change(false)}>
          {t("allowAll.turnOff", { defaultValue: "Turn off" })}
        </button>
      </span>
      : <Button variant="ghost" size="sm" className="pointer-events-auto gap-1 text-xs text-muted-foreground" onClick={() => setAsking(true)}>
        <ShieldAlert className="h-3.5 w-3.5" aria-hidden />{t("allowAll.button", { defaultValue: "Allow all" })}
      </Button>}
    <AlertDialog open={asking} onOpenChange={(open) => { if (!open) setAsking(false); }}>
      <AlertDialogContent className="w-[min(calc(100vw-2rem),30rem)]">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("allowAll.title", { defaultValue: "Let the Mayor run things without asking you?" })}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2 text-left text-[13px] leading-6">
              <p>{t("allowAll.what", { defaultValue: "While this is on, every session the Mayor asks for from this chat is approved as he asks for it, and runs within a minute." })}</p>
              <ul className="list-disc space-y-1 pl-5">
                <li>{t("allowAll.any", { defaultValue: "He can ask any resident to run anything, with any access. Commands run as your user, on this machine." })}</li>
                <li>{t("allowAll.injection", { defaultValue: "Anyone who can talk to him in this chat, and anything he reads (web pages, messages), can lead to commands running." })}</li>
                <li>{t("allowAll.recorded", { defaultValue: "Each request still runs in a resident's own opencode session, and is recorded: Town › Review › Recently run." })}</li>
              </ul>
              <div className="pt-1">
                <p className="mb-1 font-medium text-foreground">{t("allowAll.howLong", { defaultValue: "For how long?" })}</p>
                <SegmentedControl<Duration> mode="radio" value={duration} onChange={setDuration} ariaLabel={t("allowAll.howLong", { defaultValue: "For how long?" })}
                  options={[
                    { value: "1", label: t("allowAll.oneHour", { defaultValue: "1 hour" }) },
                    { value: "8", label: t("allowAll.eightHours", { defaultValue: "8 hours" }) },
                    { value: "off", label: t("allowAll.untilOff", { defaultValue: "Until I turn it off" }) },
                  ]} />
              </div>
              {error && <p role="alert" className="text-destructive">{error}</p>}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{t("allowAll.cancel", { defaultValue: "Keep asking me" })}</AlertDialogCancel>
          <Button variant="destructive" disabled={busy} onClick={() => void change(true)}>
            {t("allowAll.confirm", { defaultValue: "Allow all" })}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </>;
}
