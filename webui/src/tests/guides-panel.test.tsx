import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GuidesPanel } from "@/components/town/GuidesPanel";
import i18n from "@/i18n";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), getToken: () => "token" }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: {}, getToken: mocks.getToken }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  mocks.fetch.mockReset().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ guides: [
    { name: "letters", title: "Writing letters", when: "before your first letter in a session", tools: ["send_letter"], errors: [],
      offices: [], brought: 4, opened: 1, hint: "" },
    { name: "garden", title: "The garden", when: "when you work on the garden", tools: [], errors: [], offices: ["archivist"],
      brought: 0, opened: 5, hint: "Opened by hand more often than brought in: it may need a trigger (tools or errors)." },
    { name: "pacman-install", title: "pacman-install", when: "installing a package", tools: [], errors: [], offices: [],
      keywords: ["pacman"], kind: "skill", brought: 2, opened: 0, hint: "" },
  ] }), { status: 200, headers: { "content-type": "application/json" } })));
  vi.stubGlobal("fetch", mocks.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("guides", () => {
  it("lists the guides with what brings each in and how often, like `babo guides`", async () => {
    render(<GuidesPanel />);
    expect(await screen.findByText(/before your first letter in a session · first send_letter/)).toBeVisible();
    expect(screen.getByText(/brought in 4×, opened by hand 1×/)).toBeVisible();
    expect(screen.getByText(/it may need a trigger/)).toBeVisible();
  });

  it("lists skills with the words that bring them in", async () => {
    render(<GuidesPanel />);
    expect(await screen.findByText(/installing a package · “pacman” in a message/)).toBeVisible();
    expect(screen.getByText("pacman-install", { exact: false }).textContent).toContain("skill");
  });
});
