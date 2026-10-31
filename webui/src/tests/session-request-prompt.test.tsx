import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionRequestPrompt } from "@/components/town/SessionRequestPrompt";
import type { TownSessionRequest } from "@/lib/api";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

const nano: TownSessionRequest = {
  id: "r1", resident: "archivist", task: "Install nano.", reason: "To edit notes.", access: {},
  commands: ["sudo pacman -S nano"], requested_by: "mayor", created: "", session_key: "websocket:abc",
  digest: "d1", access_labels: {}, profile: {}, wider: ["Commands: sudo pacman -S nano"],
};
const respond = (pending: TownSessionRequest[]) => Promise.resolve(new Response(JSON.stringify({ pending }),
  { status: 200, headers: { "content-type": "application/json" } }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset();
  mocks.fetch.mockReset().mockImplementation(() => respond([]));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("session request prompt", () => {
  it("appears in the chat while a request waits, and approves exactly what it shows", async () => {
    mocks.fetch.mockImplementationOnce(() => respond([nano])).mockImplementation(() => respond([]));
    mocks.mutation.mockResolvedValue({ message: "approved", town: {} });
    render(<SessionRequestPrompt sessionKey="websocket:abc" />);
    expect(await screen.findByText("mayor asks, in this chat, for a session of archivist")).toBeVisible();
    expect(screen.getByText("sudo pacman -S nano")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Approve and run" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.decide",
      { kind: "session", id: "r1", yes: true, reason: "", digest: "d1" }, expect.any(Number)));
    await waitFor(() => expect(screen.queryByRole("alert", { name: "Waiting for your approval" })).toBeNull());
  });

  it("shows nothing when nothing waits, and pops up when a request arrives", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<SessionRequestPrompt sessionKey="websocket:abc" />);
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
    mocks.fetch.mockImplementation(() => respond([{ ...nano, session_key: "discord:42" }]));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await screen.findByText("mayor asks, from discord:42, for a session of archivist")).toBeVisible();
  });
});
