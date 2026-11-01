import { useTranslation } from "react-i18next";

export const SESSION_CHANGES = [
  { key: "files", label: "Change files" },
  { key: "shell", label: "Run commands" },
  { key: "subagents", label: "Start crew" },
  { key: "web_fetch", label: "Read web pages" },
  { key: "web_search", label: "Search the web" },
] as const;

/** What a session may do, beyond (or short of) its resident's profile: the same choices when starting one
 *  and while it runs (`babo session new --set …`, `babo session set …`). Empty means as in the profile. */
export function SessionAccessFields({ values, onChange, disabled = false }: {
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const asProfile = t("town.asProfile", { defaultValue: "as in the profile" });
  return <>
    {SESSION_CHANGES.map((c) => <label key={c.key} className="flex items-center gap-2 text-[13px]">
      {t(`town.cap.${c.key}`, { defaultValue: c.label })}
      <select className="rounded border bg-background px-1 py-0.5 text-[13px]" value={values[c.key] ?? ""} disabled={disabled}
        onChange={(event) => onChange(c.key, event.target.value)}>
        <option value="">{asProfile}</option>
        <option value="allow">{t("town.level.allow", { defaultValue: "Allow" })}</option>
        <option value="ask">{t("town.level.ask", { defaultValue: "Ask" })}</option>
        <option value="deny">{t("town.level.deny", { defaultValue: "Deny" })}</option>
      </select>
    </label>)}
    <label className="flex items-center gap-2 text-[13px]">
      {t("town.cap.read", { defaultValue: "Read" })}
      <select className="rounded border bg-background px-1 py-0.5 text-[13px]" value={values.read ?? ""} disabled={disabled}
        onChange={(event) => onChange("read", event.target.value)}>
        <option value="">{asProfile}</option>
        <option value="own">{t("town.read.own", { defaultValue: "Its own folder" })}</option>
        <option value="town">{t("town.read.town", { defaultValue: "The whole town" })}</option>
      </select>
    </label>
  </>;
}

