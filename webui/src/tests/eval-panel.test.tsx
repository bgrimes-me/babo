import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvalPanel } from "@/components/town/EvalPanel";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

const status = {
  running: null, presets: ["default", "town", "cloud"], tasks: 31,
  latest: [{ preset: "town", model: "local-35b", at: "2026-11-01T12:00:00+00:00", passed: 27, total: 31,
    baseline: { at: "2026-10-30T09:00:00+00:00", passed: 25, total: 31 } }],
};

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ ...status, running: { preset: "cloud", done: 0, total: 31, passed: 0, error: "" }, message: "Evaluating cloud: 31 tasks." });
  mocks.fetch.mockReset().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(status), { status: 200, headers: { "content-type": "application/json" } })));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("model eval", () => {
  it("shows each preset's latest score against its baseline and starts a run, like `babo eval`", async () => {
    render(<EvalPanel />);
    expect(await screen.findByText("27/31 passed")).toBeVisible();
    expect(screen.getByText(/baseline 25\/31 \(2026-10-30\)/)).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "Preset to evaluate" }), { target: { value: "cloud" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Run the eval" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.eval", { preset: "cloud", baseline: true }, expect.any(Number)));
    expect(await screen.findByRole("button", { name: "Evaluating cloud: 0/31" })).toBeDisabled();
  });
});
