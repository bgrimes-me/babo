import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResidentSessionsList } from "@/components/town/ResidentSessionsList";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: {}, getToken: mocks.getToken }) }));

const session = (id: string, title: string, extra: object = {}) => ({
  resident: "archivist", id, title, kind: "work", opencode_id: "", status: "frozen", overrides: {}, created: "2026-10-30T10:00:00",
  last_active: "2026-10-30T10:00:00", url: "", password: "", ...extra,
});
const reply = (body: object) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  try { window.localStorage.clear(); } catch { /* none */ }
  mocks.fetch.mockReset().mockImplementation(() => reply({ opencode: true, presets: [], residents: [{ id: "archivist", name: "Ada", office: "archivist", sessions: [
    session("s1", "Old notes", { last_active: "2026-10-31T09:00:00" }),
    session("s2", "Index the garden", { status: "running", live: { state: "working", activity: "", questions: [] } }),
    session("s3", "Weekly tidy", { pinned: true, tags: ["weekly"] }),
  ] }] }));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("residents' sessions in the sidebar", () => {
  it("lists them pinned first, then working, then the latest, and opens the one clicked", async () => {
    const onOpen = vi.fn();
    render(<ResidentSessionsList onOpen={onOpen} />);
    const list = await screen.findByRole("region", { name: "Residents' sessions" });
    const rows = within(list).getAllByRole("button").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Weekly tidy"), expect.stringContaining("Index the garden"), expect.stringContaining("Old notes"),
    ]);
    expect(rows[0]).toHaveTextContent("Ada · #weekly");
    expect(within(rows[1]).getByRole("img", { name: "working" })).toBeInTheDocument();
    fireEvent.click(rows[2]);
    expect(onOpen).toHaveBeenCalledWith({ resident: "archivist", id: "s1" });
  });

  it("folds away, and shows nothing without a town", async () => {
    render(<ResidentSessionsList onOpen={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Residents' sessions/ }));
    expect(screen.queryByText("Old notes")).toBeNull();
    cleanup();
    mocks.fetch.mockImplementation(() => Promise.resolve(new Response("no", { status: 404 })));
    const { container } = render(<ResidentSessionsList onOpen={() => {}} />);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(container).toBeEmptyDOMElement();
  });
});
