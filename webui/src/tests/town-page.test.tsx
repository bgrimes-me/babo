import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TownPage } from "@/components/town/TownPage";
import i18n from "@/i18n";

const town = (name = "") => ({
  name,
  root: "/home/robin/baton-town",
  residents: [{ id: "mayor", name: "", office: "mayor", status: "active", founded: "", last_active: "", unread: 1, flags: 0 }],
  board: [],
  pending: { tools: 0, proposals: 1, founding: 0, flags: 0, sessions: 1 },
  last_upkeep: null,
  upkeep: { service: "running", running: false, waiting: ["mayor: 5 hours of activity to review"], waiting_for: "someone used the model in the last 10 minutes",
    catch_up_requested: false, last_pass: "", quiet_hours: "01:00-06:00", wait_minutes: 10 },
});
const review = {
  sessions: [{
    id: "2026-10-31-archivist-abc123", resident: "archivist", task: "Install nano.", reason: "To edit notes by hand.",
    access: { web_fetch: "allow" }, commands: ["sudo pacman -S --noconfirm nano"], requested_by: "mayor", created: "",
    digest: "f00dfeed", access_labels: { web_fetch: "Read web pages" }, profile: { web_fetch: "deny" },
    wider: ["Read web pages: deny → allow", "Commands: sudo pacman -S --noconfirm nano"],
  }],
  recent: [{
    id: "2026-10-30-archivist-def456", resident: "archivist", task: "Install htop.", status: "done", decided: "",
    decided_by: "allow-all", commands: ["sudo pacman -S --noconfirm htop"], access: {}, result: "Installed htop 3.3.",
  }],
  tools: [],
  proposals: [{ id: "2026-10-29-name-the-town.md", title: "Name the town", author: "mayor", project: "", town_name: "Lantern Hill", review: "", body: "# Name the town" }],
  founding: [],
  flags: [],
};

const settings = [{
  key: "memory.idle_minutes", label: "Checkpoint after … quiet minutes", help: "A quiet session gets a checkpoint.",
  kind: "int", applies: "after Babo restarts", value: 20,
}];

const capability = (key: string, label: string, level: string, levels = ["allow", "ask", "deny"]) => ({ key, label, help: "", level, levels });
const profile = (agent: string, main: boolean, level: string) => ({
  agent, office: agent, main, saved: false,
  capabilities: [capability("shell", "Run commands", level, main ? ["allow", "deny"] : undefined)],
  mcp: [], mcp_known: [], model: "", memory: { checkpoint_every_tool_calls: null, idle_minutes: null },
  memory_defaults: { checkpoint_every_tool_calls: 15, idle_minutes: 20 },
});
const profiles = [profile("mayor", true, "allow"), profile("archivist", false, "deny")];

const sessions = {
  opencode: true,
  residents: [{ id: "archivist", name: "", office: "archivist", sessions: [{
    resident: "archivist", id: "s1", title: "Tidy the commons", kind: "work", opencode_id: "ses_1", status: "running",
    overrides: { shell: "deny" }, created: "", last_active: "", url: "http://127.0.0.1:4801", password: "pw",
  }] }],
};

const presets = [{ name: "town", model: "local-35b.gguf", default: true }, { name: "cloud", model: "gpt-cloud", default: false }];

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ message: "Proposal accepted. The town is now called Lantern Hill.", town: town("Lantern Hill") });
  mocks.fetch.mockReset().mockImplementation((url: string) => Promise.resolve(new Response(
    JSON.stringify(url.endsWith("/api/town/review") ? review : url.endsWith("/api/town/settings") ? { settings }
      : url.endsWith("/api/town/profiles") ? { profiles, presets } : url.endsWith("/api/town/sessions") ? sessions : town()),
    { status: 200, headers: { "content-type": "application/json" } },
  )));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("town page", () => {
  it("shows the residents and how much waits for review", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    expect(await screen.findByText("mayor", { selector: "span" })).toBeVisible();
    expect(screen.getByText(/The town has no name yet/)).toBeVisible();
    expect(screen.getByRole("tab", { name: "Review (2)" })).toBeVisible();
  });

  it("accepts a name proposal with the owner's reason, like `babo review`", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Review (2)" }));
    const card = (await screen.findByText("Name the town — mayor")).closest(".settings-list-row") as HTMLElement;
    expect(within(card).getByText(/Accepting names the town “Lantern Hill”/)).toBeVisible();
    fireEvent.change(within(card).getByRole("textbox"), { target: { value: "lovely" } });
    fireEvent.click(within(card).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith(
      "town.decide", { kind: "proposal", id: "2026-10-29-name-the-town.md", yes: true, reason: "lovely" }, 150_000,
    ));
    expect(await screen.findByRole("heading", { name: "Lantern Hill" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("The town is now called Lantern Hill.");
  });

  it("saves only the settings that changed, like `babo settings`", async () => {
    mocks.mutation.mockResolvedValue({ message: "Checkpoint after … quiet minutes: 20 → 30", settings: [{ ...settings[0], value: 30 }] });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Settings" }));
    const input = await screen.findByDisplayValue("20");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.settings", { changes: { "memory.idle_minutes": "30" } }, 20_000));
    expect(await screen.findByRole("status")).toHaveTextContent("20 → 30");
  });

  it("marks the Mayor as the main agent and changes a resident's profile, like `babo profile`", async () => {
    mocks.mutation.mockResolvedValue({
      message: "Profile of archivist: Run commands: deny → ask. It applies from the agent's next session.",
      profiles: [profiles[0], profile("archivist", false, "ask")],
    });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Profiles" }));
    const mayor = await screen.findByRole("region", { name: "Mayor" });
    expect(within(mayor).getByText("Main agent")).toBeVisible();
    expect(within(mayor).queryByRole("radio", { name: "Ask" })).toBeNull();
    const archivist = screen.getByRole("region", { name: "archivist · archivist" });
    fireEvent.click(within(archivist).getByRole("radio", { name: "Ask" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.profile", { agent: "archivist", changes: { shell: "ask" } }, expect.any(Number)));
    expect(await screen.findByRole("status")).toHaveTextContent("Run commands: deny → ask");
  });

  it("gives a resident a model preset from Settings, like `babo profile archivist model cloud`", async () => {
    mocks.mutation.mockResolvedValue({ message: "Profile of archivist: Model: Babo default → cloud.", profiles, presets });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Profiles" }));
    const archivist = await screen.findByRole("region", { name: "archivist · archivist" });
    const model = within(archivist).getByRole("combobox", { name: /Model/ });
    expect(within(model).getByRole("option", { name: "Babo's default (town)" })).toBeInTheDocument();
    fireEvent.change(model, { target: { value: "cloud" } });
    fireEvent.click(within(archivist).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.profile", { agent: "archivist", changes: { model: "cloud" } }, expect.any(Number)));
  });

  it("shows what upkeep is waiting for and catches up on request, like `babo upkeep --now`", async () => {
    mocks.mutation.mockResolvedValue({ message: "Catch-up requested: upkeep starts within a minute and does everything that is waiting.", town: town() });
    render(<TownPage onBackToChat={() => {}} />);
    expect(await screen.findByText("Waiting, because someone used the model in the last 10 minutes.")).toBeVisible();
    expect(screen.getByText("mayor: 5 hours of activity to review")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Catch up now" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.upkeep", {}, expect.any(Number)));
    expect(await screen.findByRole("status")).toHaveTextContent("Catch-up requested");
  });

  it("lists residents' sessions in opencode and starts one with its own options, like `babo session new`", async () => {
    mocks.mutation.mockResolvedValue({ ...sessions, message: "archivist's session “Index the notes” is running." });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Sessions" }));
    const archivist = await screen.findByRole("region", { name: "archivist" });
    expect(within(archivist).getByText("Tidy the commons")).toBeVisible();
    expect(within(archivist).getByRole("link", { name: "Open" })).toHaveAttribute("href", "http://127.0.0.1:4801");
    expect(within(archivist).getByText(/password pw/)).toBeVisible();
    fireEvent.change(within(archivist).getByRole("textbox"), { target: { value: "Index the notes" } });
    fireEvent.change(within(archivist).getByRole("combobox", { name: "Search the web" }), { target: { value: "allow" } });
    fireEvent.click(within(archivist).getByRole("button", { name: "New session" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.session.new",
      { resident: "archivist", title: "Index the notes", overrides: { web_search: "allow" } }, 120_000));
    expect(await screen.findByRole("status")).toHaveTextContent("is running");
  });

  it("removes a broken session only after asking, like `babo session remove`", async () => {
    mocks.mutation.mockResolvedValue({ opencode: true, residents: [{ ...sessions.residents[0], sessions: [] }],
      message: "Removed archivist's session “Tidy the commons”." });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Sessions" }));
    const archivist = await screen.findByRole("region", { name: "archivist" });
    fireEvent.click(within(archivist).getByRole("button", { name: "Remove" }));
    expect(within(archivist).getByText(/without being reviewed into memory/)).toBeVisible();
    expect(mocks.mutation).not.toHaveBeenCalled();
    fireEvent.click(within(archivist).getByRole("button", { name: "Remove for good" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.session.remove", { resident: "archivist", id: "s1" }, 120_000));
    await waitFor(() => expect(within(archivist).queryByText("Tidy the commons")).toBeNull());
  });

  it("shows a session request word for word and approves exactly what was shown", async () => {
    mocks.mutation.mockResolvedValue({ message: "Request approved: archivist will run it as soon as upkeep picks it up (within a minute).", town: town() });
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Review (2)" }));
    const card = (await screen.findByText("mayor asks for a session of archivist")).closest(".settings-list-row") as HTMLElement;
    expect(within(card).getByText("Install nano.")).toBeVisible();
    expect(within(card).getByText("sudo pacman -S --noconfirm nano")).toBeVisible();
    expect(within(card).getByText(/Beyond its profile: Read web pages: deny → allow/)).toBeVisible();
    fireEvent.change(within(card).getByRole("textbox"), { target: { value: "go ahead" } });
    fireEvent.click(within(card).getByRole("button", { name: "Approve and run" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.decide",
      { kind: "session", id: "2026-10-31-archivist-abc123", yes: true, reason: "go ahead", digest: "f00dfeed" }, 150_000));
  });

  it("lists what ran recently, and says when allow-all approved it", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Review (2)" }));
    expect(await screen.findByText(/approved automatically \(allow all\)/)).toBeVisible();
    expect(screen.getByText("Installed htop 3.3.")).toBeVisible();
  });
});
