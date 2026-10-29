import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TownPage } from "@/components/town/TownPage";
import i18n from "@/i18n";

const town = (name = "") => ({
  name,
  root: "/home/squid/baton-town",
  residents: [{ id: "mayor", name: "", office: "mayor", status: "active", founded: "", last_active: "", unread: 1, flags: 0 }],
  board: [],
  pending: { tools: 0, proposals: 1, founding: 0, flags: 0 },
  last_night: null,
});
const review = {
  tools: [],
  proposals: [{ id: "2026-10-29-name-the-town.md", title: "Name the town", author: "mayor", project: "", town_name: "Lantern Hill", review: "", body: "# Name the town" }],
  founding: [],
  flags: [],
};

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ message: "Proposal accepted. The town is now called Lantern Hill.", town: town("Lantern Hill") });
  mocks.fetch.mockReset().mockImplementation((url: string) => Promise.resolve(new Response(
    JSON.stringify(url.endsWith("/api/town/review") ? review : town()),
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
    expect(screen.getByRole("tab", { name: "Review (1)" })).toBeVisible();
  });

  it("accepts a name proposal with Squid's reason, like `babo review`", async () => {
    render(<TownPage onBackToChat={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Review (1)" }));
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
});
