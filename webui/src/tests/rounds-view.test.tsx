import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RoundsView } from "@/components/town/RoundsView";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

const data = {
  broken: { draft: "a round has at least one step" },
  babo: [{ title: "Memory reviews", what: "each resident reviews what it did" }],
  rounds: [{
    name: "night", title: "The night round", about: "The town's work that used to be part of upkeep.",
    when: "whenever there's work for it, in each upkeep pass", runs: "wait", enabled: true,
    last_run: "2026-11-01T03:10:00+00:00", last_status: "done", next_run: "",
    steps: [
      { office: "archivist", duty: "Curate the queue.", material: "promotions", fallback: "mayor", holder: "archivist", access: {}, commands: [] },
      { office: "inspector", duty: "Review the changes.", material: "inspection", fallback: "", holder: "", access: { shell: "deny" }, commands: [] },
    ],
    rehearsal: { id: "night--2026-11-01-180000", round: "night", live: false, started: "2026-11-01T18:00:00+00:00", finished: "x",
      status: "done", approved: "",
      steps: [{ office: "archivist", resident: "archivist", status: "rehearsed", note: "", report: "", stopped: true,
        asked: [{ action: "shell", resources: ["pacman -Q nano"], decision: "refused" }] },
        { office: "inspector", resident: "", status: "skipped", note: "vacancy: nobody holds this office", report: "", stopped: false, asked: [] }],
      proposal: [{ office: "archivist", access: {}, commands: ["pacman -Q nano"], cannot: [] }, { office: "inspector", access: {}, commands: [], cannot: [] }] },
    recent: [{ round: "night", trigger: "in this upkeep pass", started: "", finished: "", status: "done",
      steps: [{ office: "archivist", resident: "archivist", status: "done", note: "", report: "Curated 2 notes." },
        { office: "inspector", resident: "", status: "skipped", note: "vacancy: nobody holds this office", report: "" }] }],
  }],
};

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ ...data, message: "“The night round” starts within a minute." });
  mocks.fetch.mockReset().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } })));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("rounds", () => {
  it("rehearses a round, shows what each step asked for, and approves the access it proposes, like `babo rounds rehearse|approve`", async () => {
    render(<RoundsView onNotice={() => {}} onError={() => {}} />);
    const night = await screen.findByRole("region", { name: "The night round" });
    fireEvent.click(within(night).getByRole("button", { name: "Rehearse live" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.rehearse", { name: "night", live: true }, expect.any(Number)));
    const report = within(night).getByRole("region", { name: "Rehearsal report" });
    expect(report).toHaveTextContent("asked to shell: pacman -Q nano (refused)");
    expect(report).toHaveTextContent("proposes: may run `pacman -Q nano`");
    expect(report).toHaveTextContent("approve this much and rehearse again");
    fireEvent.click(within(report).getByRole("button", { name: "Approve this access" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.approve",
      { name: "night", rehearsal: "night--2026-11-01-180000" }, expect.any(Number)));
  });

  it("sets what a step may do by hand, like `babo rounds access`", async () => {
    render(<RoundsView onNotice={() => {}} onError={() => {}} />);
    const night = await screen.findByRole("region", { name: "The night round" });
    expect(within(night).getByText("What it may do: shell: deny")).toBeInTheDocument();
    fireEvent.change(within(night).getAllByRole("combobox", { name: "Run commands" })[0], { target: { value: "allow" } });
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.access",
      { name: "night", step: 1, access: { shell: "allow" } }, expect.any(Number)));
    fireEvent.change(within(night).getAllByRole("textbox", { name: "Exact commands it may run, one per line" })[0], { target: { value: "pacman -Q nano\n" } });
    const save = within(night).getAllByRole("button", { name: "Save the commands" })[0];
    await waitFor(() => expect(save).toBeEnabled());  // once the change before has gone through
    fireEvent.click(save);
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.access",
      { name: "night", step: 1, access: {}, commands: ["pacman -Q nano"] }, expect.any(Number)));
  });

  it("shows each round's steps, who does them, a vacancy, the last run's reports, and Babo's own work", async () => {
    const notice = vi.fn();
    render(<RoundsView onNotice={notice} onError={() => {}} />);
    const night = await screen.findByRole("region", { name: "The night round" });
    expect(within(night).getByText(/Runs whenever there's work for it/)).toBeVisible();
    expect(within(night).getByText(/vacancy: waits until someone moves in/)).toBeVisible();
    expect(within(night).getByText("Curated 2 notes.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("rounds/draft.md can't be read: a round has at least one step");
    expect(screen.getByRole("region", { name: "Part of Babo" })).toHaveTextContent("Memory reviews");
    fireEvent.click(within(night).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.run", { name: "night" }, expect.any(Number)));
    expect(notice).toHaveBeenCalledWith("“The night round” starts within a minute.");
  });

  it("changes when a round runs, with the warning, and turns it off, like `babo rounds set`", async () => {
    mocks.mutation.mockResolvedValue({ ...data, rounds: [{ ...data.rounds[0], runs: "now" }], message: "ok" });
    render(<RoundsView onNotice={() => {}} onError={() => {}} />);
    const night = await screen.findByRole("region", { name: "The night round" });
    fireEvent.change(within(night).getByRole("combobox", { name: "When it's due" }), { target: { value: "now" } });
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.set", { name: "night", runs: "now" }, expect.any(Number)));
    expect(await within(night).findByRole("note")).toHaveTextContent("shares the model with your chats");
    fireEvent.click(within(night).getByRole("switch", { name: "On" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.rounds.set", { name: "night", enabled: false }, expect.any(Number)));
  });
});
