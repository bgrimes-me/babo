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
      { office: "archivist", duty: "Curate the queue.", material: "promotions", fallback: "mayor", holder: "archivist" },
      { office: "inspector", duty: "Review the changes.", material: "inspection", fallback: "", holder: "" },
    ],
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
