import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AllowAllControl } from "@/components/town/AllowAllControl";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

const respond = (active: Record<string, unknown>) => Promise.resolve(new Response(JSON.stringify({ active }),
  { status: 200, headers: { "content-type": "application/json" } }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset();
  mocks.fetch.mockReset().mockImplementation(() => respond({}));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("allow all", () => {
  it("is off until Squid confirms, says what it means, and asks for how long", async () => {
    mocks.mutation.mockResolvedValue({ message: "on", active: { "websocket:abc": { since: "", until: "", by: "squid" } } });
    render(<AllowAllControl sessionKey="websocket:abc" />);
    fireEvent.click(await screen.findByRole("button", { name: "Allow all" }));
    expect(await screen.findByText(/can lead to commands running/)).toBeVisible();
    expect(mocks.mutation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "Until I turn it off" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Allow all" }).at(-1)!);
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.allow_all", { key: "websocket:abc", on: true, hours: null }, expect.any(Number)));
    expect(await screen.findByText("Allow all is on")).toBeVisible();
  });

  it("shows when it's on for this chat, and turns off in one click", async () => {
    mocks.fetch.mockImplementation(() => respond({ "websocket:abc": { since: "", until: "", by: "squid" } }));
    mocks.mutation.mockResolvedValue({ message: "off", active: {} });
    render(<AllowAllControl sessionKey="websocket:abc" />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.allow_all", { key: "websocket:abc", on: false, hours: null }, expect.any(Number)));
    expect(await screen.findByRole("button", { name: "Allow all" })).toBeVisible();
  });
});
