import { useEffect, useState } from "react";
import { Loader2, Plus, UserPlus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SettingsGroup } from "@/components/settings/shared/SettingsControls";
import { SessionAccessFields } from "@/components/town/AccessFields";
import { useClient } from "@/providers/ClientProvider";
import {
  fetchTownRounds, moveInToTown,
  type TownChange, type TownJoin, type TownOffice, type TownOverview,
} from "@/lib/api";

const NEW = "";

/** The town's offices nobody holds (vacancies), and moving a newcomer in: into one of them, or into a new office
 *  with its role, the access it starts with, and the rounds it joins. The same as `babo move-in`. */
export function OfficesPanel({ town, onChanged, onError }: {
  town: TownOverview;
  onChanged: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [moving, setMoving] = useState<string | null>(null); // an office's name, NEW for a new office, null: closed
  const offices = town.offices?.offices ?? [];
  const vacancies = offices.filter((o) => o.vacant);
  return <>
    <h2 className="settings-list-inset mt-2 text-[13px] font-medium text-muted-foreground">{t("town.vacancies", { defaultValue: "Vacancies" })}</h2>
    <SettingsGroup>
      {vacancies.length === 0 && <p className="settings-list-row py-3 text-[13px] text-muted-foreground">
        {t("town.noVacancies", { defaultValue: "Every office is held." })}
      </p>}
      {vacancies.map((o) => <div key={o.name} className="settings-list-row py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="min-w-0 flex-1">
            <span className="block text-[14px] font-medium">{o.title}</span>
            <span className="mt-0.5 block text-xs text-muted-foreground">
              {o.joins.length
                ? t("town.officeRounds", { defaultValue: "Takes part in: {{rounds}}", rounds: o.joins.map((j) => j.round).join(", ") })
                : t("town.officeNoRounds", { defaultValue: "Not in any round yet" })}
            </span>
          </span>
          <Button variant="outline" size="sm" className="gap-1" onClick={() => setMoving(moving === o.name ? null : o.name)}>
            <UserPlus className="h-4 w-4" />{t("town.moveSomeoneIn", { defaultValue: "Move someone in" })}
          </Button>
        </div>
        {moving === o.name && <MoveInForm office={o} town={town} onDone={(change) => { setMoving(null); onChanged(change); }} onError={onError} />}
      </div>)}
      <div className="settings-list-row py-3">
        <Button variant="outline" size="sm" className="gap-1" onClick={() => setMoving(moving === NEW ? null : NEW)}>
          <Plus className="h-4 w-4" />{t("town.newOfficeButton", { defaultValue: "A new office" })}
        </Button>
        {moving === NEW && <MoveInForm office={null} town={town} onDone={(change) => { setMoving(null); onChanged(change); }} onError={onError} />}
      </div>
    </SettingsGroup>
  </>;
}

function MoveInForm({ office, town, onDone, onError }: {
  office: TownOffice | null;
  town: TownOverview;
  onDone: (change: TownChange) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const { client, getToken } = useClient();
  const [name, setName] = useState(office?.name ?? "");
  const [role, setRole] = useState("");
  const [profile, setProfile] = useState<Record<string, string>>({});
  const [joins, setJoins] = useState<TownJoin[]>([]);
  const [rounds, setRounds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    fetchTownRounds(getToken()).then((data) => { if (live) setRounds(data.rounds.map((r) => r.name)); }).catch(() => undefined);
    return () => { live = false; };
  }, [getToken]);
  const taken = (town.offices?.offices ?? []).some((o) => o.name === name.trim().toLowerCase());
  const ready = !!name.trim() && (office !== null || (!!role.trim() && !taken)) && joins.every((j) => j.round.trim() && j.duty.trim());

  const submit = async () => {
    setBusy(true);
    try {
      const move = office
        ? { office: office.name, ...(joins.length ? { joins } : {}) }
        : { office: name.trim(), role, profile, joins };
      onDone(await moveInToTown(client, move));
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  const setJoin = (index: number, change: Partial<TownJoin>) => setJoins((all) => all.map((j, i) => i === index ? { ...j, ...change } : j));

  return <form className="mt-3 flex flex-col gap-3 rounded-md border p-3" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    {office
      ? <p className="text-[13px] text-muted-foreground">
        {t("town.moveInto", { defaultValue: "The newcomer starts with the office's role and access, and joins its rounds. You can add more rounds below." })}
      </p>
      : <>
        <label className="flex flex-col gap-1 text-[13px]">
          {t("town.officeName", { defaultValue: "Office (a short name, e.g. gardener)" })}
          <Input value={name} maxLength={28} onChange={(event) => setName(event.target.value)} />
          {taken && <span className="text-xs text-destructive">{t("town.officeTaken", { defaultValue: "The town has this office: move someone into it above." })}</span>}
        </label>
        <label className="flex flex-col gap-1 text-[13px]">
          {t("town.officeRole", { defaultValue: "Its role: what it does, and what isn't its job" })}
          <textarea className="min-h-24 rounded-md border bg-background px-2 py-1 text-[13px]" value={role} maxLength={20000}
            onChange={(event) => setRole(event.target.value)} />
        </label>
        <fieldset className="flex flex-col gap-1">
          <legend className="mb-1 text-[13px]">{t("town.officeAccess", { defaultValue: "What it may do, beyond a new resident's profile" })}</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            <SessionAccessFields values={profile} disabled={busy} onChange={(key, value) => setProfile((current) => {
              const next = { ...current };
              if (value) next[key] = value; else delete next[key];
              return next;
            })} />
          </div>
        </fieldset>
      </>}
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-[13px]">{t("town.joinsRounds", { defaultValue: "Rounds it joins" })}</legend>
      {joins.map((j, index) => <div key={index} className="flex flex-wrap items-center gap-2">
        <select aria-label={t("town.joinRound", { defaultValue: "Round" })} className="rounded border bg-background px-1 py-1 text-[13px]"
          value={j.round} onChange={(event) => setJoin(index, { round: event.target.value })}>
          <option value="">{t("town.pickRound", { defaultValue: "Pick a round" })}</option>
          {rounds.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <Input className="min-w-48 flex-1" aria-label={t("town.joinDuty", { defaultValue: "Its duty there" })} value={j.duty} maxLength={2000}
          placeholder={t("town.joinDuty", { defaultValue: "Its duty there" })} onChange={(event) => setJoin(index, { duty: event.target.value })} />
        <Button type="button" variant="ghost" size="sm" onClick={() => setJoins((all) => all.filter((_, i) => i !== index))}>
          {t("town.remove", { defaultValue: "Remove" })}
        </Button>
      </div>)}
      <div>
        <Button type="button" variant="outline" size="sm" className="gap-1" disabled={!rounds.length}
          onClick={() => setJoins((all) => [...all, { round: "", duty: "" }])}>
          <Plus className="h-4 w-4" />{t("town.addRound", { defaultValue: "Add a round" })}
        </Button>
      </div>
    </fieldset>
    <p className="text-xs text-muted-foreground">
      {t("town.foundingCost", { defaultValue: "A new resident uses compute on your machine every day it is active." })}
    </p>
    <div>
      <Button type="submit" disabled={busy || !ready}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : t("town.moveIn", { defaultValue: "Move in" })}
      </Button>
    </div>
  </form>;
}
