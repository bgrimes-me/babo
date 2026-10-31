import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NoticesPanel } from "@/components/town/NoticesPanel";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ mutation: vi.fn(), fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: mocks.mutation }, getToken: mocks.getToken }) }));

const notice = { id: "n1", at: "2026-10-31T21:00:00+00:00", kind: "wrapped_up", resident: "archivist", session: "s1",
  text: "archivist wrapped up “Index the notes” and wrote its memory." };

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.mutation.mockReset().mockResolvedValue({ notices: [] });
  mocks.fetch.mockReset().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ notices: [notice] }),
    { status: 200, headers: { "content-type": "application/json" } })));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("notices", () => {
  it("shows what the owner would want to know until it is read, like `babo notices --read`", async () => {
    render(<NoticesPanel />);
    expect(await screen.findByText(/archivist wrapped up “Index the notes”/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(mocks.mutation).toHaveBeenCalledWith("town.notices.read", {}, expect.any(Number)));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Notices" })).toBeNull());
  });
});
