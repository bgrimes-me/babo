import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { useClient } from "@/providers/ClientProvider";
import { fetchTownGuides, type TownGuide } from "@/lib/api";

/** The guides Babo brings in at the right moment: what brings each in, and how often. The same as `babo guides`. */
export function GuidesPanel() {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const [guides, setGuides] = useState<TownGuide[] | null>(null);
  useEffect(() => { fetchTownGuides(getToken()).then((r) => setGuides(r.guides ?? [])).catch(() => setGuides(null)); }, [getToken]);
  if (!guides || guides.length === 0) return null;
  return <section aria-label={t("town.guidesTitle", { defaultValue: "Guides" })} className="mt-4">
    <h2 className="settings-list-inset text-[13px] font-medium text-foreground">{t("town.guidesTitle", { defaultValue: "Guides" })}</h2>
    <p className="settings-list-inset text-[13px] leading-6 text-muted-foreground">
      {t("town.guidesIntro", { defaultValue: "Short notes Babo brings in when they're needed: with the first use of a tool in a session, or after a refusal. Every agent sees their names; any can open one by hand. They're notes in commons/guides: change them or add your own." })}
    </p>
    <SettingsGroup>
      {guides.map((g) => <div key={g.name} className="settings-list-row py-3 text-[13px]">
        <span className="block font-medium">{g.title} <span className="font-normal text-muted-foreground">· {g.name}</span></span>
        <span className="mt-0.5 block text-xs text-muted-foreground">
          {g.when}
          {" · "}{[...g.tools.map((tool) => t("town.guideTool", { defaultValue: "first {{tool}}", tool })), ...(g.errors.length ? [t("town.guideError", { defaultValue: "an error" })] : [])].join(", ")}
          {g.offices.length ? ` · ${g.offices.join(", ")}` : ""}
          {" · "}{t("town.guideCounts", { defaultValue: "brought in {{brought}}×, opened by hand {{opened}}×", brought: g.brought, opened: g.opened })}
        </span>
        {g.hint && <span className="mt-0.5 block text-xs text-amber-600 dark:text-amber-400">{g.hint}</span>}
      </div>)}
    </SettingsGroup>
  </section>;
}
