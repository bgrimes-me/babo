import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TasksPanel } from "@/components/town/TasksPanel";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: {}, getToken: mocks.getToken }) }));

const respond = (lists: unknown[]) => mocks.fetch.mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ lists }),
  { status: 200, headers: { "content-type": "application/json" } })));

beforeEach(async () => { await i18n.changeLanguage("en"); mocks.fetch.mockReset(); vi.stubGlobal("fetch", mocks.fetch); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("task lists", () => {
  it("shows what each agent is working on, like `babo tasks`", async () => {
    respond([{ resident: "mayor", session: "websocket:abc", updated: "", done: 1, total: 2, open: 1,
      items: [{ task: "Check the board", status: "done" }, { task: "Write to Robin", status: "doing" }] }]);
    render(<TasksPanel />);
    expect(await screen.findByRole("region", { name: "Working on" })).toBeVisible();
    expect(screen.getByText("1 of 2 done", { exact: false })).toBeVisible();
    expect(screen.getByText("Check the board")).toHaveClass("line-through");
    expect(screen.getByLabelText("doing")).toBeInTheDocument();
  });

  it("shows nothing when nobody has anything open", async () => {
    respond([]);
    const { container } = render(<TasksPanel />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container).toBeEmptyDOMElement();
  });
});
