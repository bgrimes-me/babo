import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, CircleDashed } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

import { ActivityStep } from "./ActivityStep";
import { compactReasoningPreview } from "./reasoning-preview";

export function ReasoningRow({
  text,
  streaming,
  className,
}: {
  text: string;
  streaming: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const fallback = streaming
    ? t("message.reasoningStreaming", { defaultValue: "Thinking…" })
    : t("message.reasoning", { defaultValue: "Thinking" });
  const preview = compactReasoningPreview(text) || fallback;
  // CSS ellipsis still lays out the full string, including the animated copy.
  const truncated = preview.length > 512;
  const label = truncated
    ? preview.slice(0, 512).replace(/[\uD800-\uDBFF]$/, "") + "…"
    : preview;
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const full = text.trim();
  return (
    <div className={cn("flex min-w-0 flex-col", className)} data-testid="reasoning-row">
      <div className="flex min-w-0 items-start gap-2">
        <ActivityStep
          marker={<ReasoningMarker streaming={streaming} />}
          active={streaming}
          animateLabel={!truncated}
          tone={streaming ? "active" : "success"}
          label={label}
          tooltipContent={preview}
          labelClassName="italic text-muted-foreground/78"
          contentClassName="overflow-hidden"
          className="min-w-0 flex-1"
        />
        {full && <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}
          className={cn(
            "inline-flex h-6 shrink-0 items-center gap-1 rounded-md border border-violet-500/25 bg-violet-500/[0.06] px-2",
            "text-[11px] font-medium uppercase tracking-wide text-violet-700/80 transition-colors hover:bg-violet-500/[0.12]",
            "dark:text-violet-300/80",
          )}
        >
          {t("message.reasoningLabel", { defaultValue: "Thinking" })}
          <ChevronDown className={cn("h-3 w-3 transition-transform duration-200", open && "rotate-180")} aria-hidden />
        </button>}
      </div>
      {open && full && <div
        id={panelId}
        role="region"
        aria-label={t("message.reasoningPanel", { defaultValue: "The model's thinking" })}
        className={cn(
          "mt-1 max-h-96 overflow-y-auto rounded-md border-l-2 border-violet-500/40 bg-violet-500/[0.05] px-3 py-2",
          "whitespace-pre-wrap text-[13px] italic leading-6 text-muted-foreground",
          "animate-in fade-in-0 slide-in-from-top-1 duration-200 motion-reduce:animate-none",
        )}
      >
        {full}
      </div>}
    </div>
  );
}

function ReasoningMarker({ streaming }: { streaming: boolean }) {
  const wasStreamingRef = useRef(streaming);
  const [justCompleted, setJustCompleted] = useState(false);

  useEffect(() => {
    if (wasStreamingRef.current && !streaming) {
      setJustCompleted(true);
      const timeout = window.setTimeout(() => setJustCompleted(false), 300);
      wasStreamingRef.current = streaming;
      return () => window.clearTimeout(timeout);
    }
    wasStreamingRef.current = streaming;
    return undefined;
  }, [streaming]);

  if (streaming) {
    return (
      <CircleDashed
        data-testid="activity-reasoning-marker"
        data-state="thinking"
        className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground/55"
        strokeWidth={1.8}
        aria-hidden
      />
    );
  }
  return (
    <span
      data-testid="activity-reasoning-marker"
      data-state="done"
      className={cn(
        "grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border border-emerald-500/28 text-emerald-500/78",
        "bg-emerald-500/[0.035] transition-[border-color,background-color,box-shadow,transform] duration-300 ease-out",
        justCompleted
          && "animate-in fade-in-0 zoom-in-75 shadow-[0_0_0_3px_rgba(16,185,129,0.10)] motion-reduce:animate-none",
      )}
      aria-hidden
    >
      <Check
        className={cn(
          "h-2.5 w-2.5 stroke-[2.4]",
          justCompleted && "animate-in fade-in-0 zoom-in-50 duration-300 motion-reduce:animate-none",
        )}
      />
    </span>
  );
}
