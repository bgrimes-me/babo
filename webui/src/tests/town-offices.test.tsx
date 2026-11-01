import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TownPage } from "@/components/town/TownPage";
import i18n from "@/i18n";

const office = (name: string, vacant: boolean, joins: { round: string; duty: string }[] = []) => ({
  name, title: name[0].toUpperCase() + name.slice(1), role: `# Role: ${name}`, profile: {}, joins, holders: vacant ? [] : [name], vacant,
});
const town = {
  name: "Lantern Hill",
  root: "/home/robin/baton-town",
  residents: [
    { id: "mayor", name: "", office: "mayor", status: "active", founded: "", last_active: "", unread: 0, flags: 0 },
    { id: "archivist", name: "", office: "archivist", status: "active", founded: "", last_active: "", unread: 0, flags: 0 },
  ],
  board: [],
  pending: { tools: 0, proposals: 0, founding: 1, flags: 0, sessions: 0 },
  last_upkeep: null,
  upkeep: { service: "running", running: false, waiting: [], waiting_for: "", catch_up_requested: false, last_pass: "", quiet_hours: "", wait_minutes: 10 },
  offices: { offices: [office("archivist", false), office("inspector", true, [{ round: "night", duty: "Inspect the day's work." }])], broken: {} },
};
const rounds = { rounds: [{ name: "night", title: "The night round", steps: [] }, { name: "paper", title: "The paper round", steps: [] }], broken: {}, babo: [] };
const review = {
  sessions: [], recent: [], tools: [], proposals: [], flags: [],
  founding: [{ id: "2026-11-01-move_in_request-sysadmin-mayor.md", office: "sysadmin", author: "mayor", body: "Someone to watch the machine.",
    profile: { shell: "ask" }, joins: [{ round: "night", duty: "Check the disks." }] }],
};

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ message: "inspector moved in as the inspector.", town });
  mocks.fetch.mockReset().mockImplementation((url: string) => Promise.resolve(new Response(
    JSON.stringify(url.endsWith("/api/town/rounds") ? rounds : url.endsWith("/api/town/review") ? review : town),
    { status: 200, headers: { "content-type": "application/json" } },
  )));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("offices and moving in", () => {
  it("lists vacancies and moves someone into one, like `babo move-in`", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    const row = (await screen.findByText("Inspector")).closest(".settings-list-row") as HTMLElement;
    expect(within(row).getByText("Takes part in: night")).toBeVisible();
    expect(screen.queryByText("Archivist", { selector: "span" })).toBeNull(); // held: not a vacancy
    fireEvent.click(within(row).getByRole("button", { name: "Move someone in" }));
    fireEvent.click(within(row).getByRole("button", { name: "Move in" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.move_in", { office: "inspector" }, expect.any(Number)));
    expect(await screen.findByRole("status")).toHaveTextContent("inspector moved in");
  });

  it("moves a newcomer into a new office with its role, access and rounds", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "A new office" }));
    const form = screen.getByRole("button", { name: "Move in" }).closest("form") as HTMLElement;
    expect(within(form).getByRole("button", { name: "Move in" })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText(/Office \(a short name/), { target: { value: "archivist" } });
    expect(within(form).getByText(/The town has this office/)).toBeVisible();
    fireEvent.change(within(form).getByLabelText(/Office \(a short name/), { target: { value: "reporter" } });
    fireEvent.change(within(form).getByLabelText(/Its role/), { target: { value: "You write the paper." } });
    fireEvent.change(within(form).getByLabelText("Read web pages"), { target: { value: "allow" } });
    await waitFor(() => expect(within(form).getByRole("button", { name: "Add a round" })).toBeEnabled());
    fireEvent.click(within(form).getByRole("button", { name: "Add a round" }));
    fireEvent.change(within(form).getByLabelText("Round"), { target: { value: "paper" } });
    expect(within(form).getByRole("button", { name: "Move in" })).toBeDisabled(); // a round needs its duty
    fireEvent.change(within(form).getByRole("textbox", { name: "Its duty there" }), { target: { value: "Write the edition." } });
    fireEvent.click(within(form).getByRole("button", { name: "Move in" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.move_in", {
      office: "reporter", role: "You write the paper.", profile: { web_fetch: "allow" }, joins: [{ round: "paper", duty: "Write the edition." }],
    }, expect.any(Number)));
  });

  it("asks before a resident moves away, and never offers it for the Mayor", async () => {
    mocks.mutation.mockResolvedValue({ message: "archivist has moved away.", town });
    render(<TownPage onBackToChat={() => {}} />);
    const buttons = await screen.findAllByRole("button", { name: "Move away" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    const confirm = screen.getByRole("group");
    expect(confirm).toHaveTextContent("its office becomes a vacancy");
    fireEvent.click(within(confirm).getByRole("button", { name: "Move away" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.move_away", { resident: "archivist" }, expect.any(Number)));
  });

  it("shows the access and rounds a move-in request asks for", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Review (1)" }));
    const card = (await screen.findByText(/New office “sysadmin”/)).closest(".settings-list-row") as HTMLElement;
    expect(within(card).getByText("shell: ask")).toBeVisible();
    expect(within(card).getByText("Check the disks.", { exact: false })).toBeVisible();
    expect(within(card).getByRole("button", { name: "Move in" })).toBeVisible();
  });
});
