import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { AnswerResponse } from "./api";

const answer: AnswerResponse = {
  run_id: "run_0123456789abcdef01234567",
  trace_id: "a".repeat(32),
  conversation_id: "c1",
  status: "answered",
  mode: "standard",
  claims: [{ text: "The New York per diem is 95 USD.", citation_ids: ["chk_1"] }],
  citations: [
    {
      citation_id: "chk_1",
      evidence_id: "E1",
      document_id: "doc_1",
      document_version: "v_1",
      title: "Travel & Expense Policy (2024)",
      location: "line 9",
      section: "Travel and Expense Policy > Per Diem Rates",
      source_uri: "file://x",
      open_url: "/api/citations/chk_1",
      source_modified_at: "2024-01-02T00:00:00Z",
      acl_synced_at: "2026-09-30T00:00:00Z",
      is_latest_revision: true,
    },
  ],
  conflicts: [],
  outdated_sources: [],
  abstain_reason: null,
  judge_status: "skipped",
  from_cache: false,
  inference_mode: "fixture",
};

function mockFetch(routes: Record<string, () => unknown>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const key = Object.keys(routes).find((k) => url.endsWith(k));
      if (!key) return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
      return new Response(JSON.stringify(routes[key]!()), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

const me = { display_name: "Alice", tenant_id: "t1", clearance: "confidential", projects: [], is_admin: false, group_count: 2, auth_provider: "dev", inference_mode: "fixture" };
const devUsers = { synthetic: true, users: [{ username: "alice@acme.example", display_name: "Alice", description: "Finance" }] };

afterEach(() => vi.unstubAllGlobals());

async function signIn() {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: /Alice/ }));
  await screen.findByLabelText(/Ask a question/);
}

describe("App", () => {
  it("shows answer with citations and never sends authorization filters", async () => {
    const calls = mockFetch({ "/dev-idp/users": () => devUsers, "/dev-idp/token": () => ({ access_token: "tok" }), "/api/me": () => me, "/api/ask": () => answer });
    await signIn();
    expect(screen.getByText(/FIXTURE models/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Ask a question/), "What is the New York per diem?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/95 USD/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open source 1/ })).toBeInTheDocument();
    expect(screen.getByText(/permissions synced/)).toBeInTheDocument();
    const askCall = calls.find((c) => c.url.endsWith("/api/ask"))!;
    const body = JSON.parse(String(askCall.init?.body));
    expect(Object.keys(body).sort()).toEqual(["mode", "question"]);
    expect(new Headers(askCall.init?.headers).get("Authorization")).toBe("Bearer tok");
  });

  it("renders an accessible abstention state", async () => {
    mockFetch({ "/dev-idp/users": () => devUsers, "/dev-idp/token": () => ({ access_token: "tok" }), "/api/me": () => me, "/api/ask": () => ({ ...answer, status: "abstained", claims: [], citations: [], abstain_reason: "insufficient_evidence" }) });
    await signIn();
    await userEvent.type(screen.getByLabelText(/Ask a question/), "Unknown?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/couldn't find enough evidence/)).toBeInTheDocument();
    const results = await axe.run(document.body, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });

  it("shows a loading status while waiting and an error with trace id on failure", async () => {
    let resolveAsk: (v: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/dev-idp/users")) return new Response(JSON.stringify(devUsers));
        if (url.endsWith("/dev-idp/token")) return new Response(JSON.stringify({ access_token: "tok" }));
        if (url.endsWith("/api/me")) return new Response(JSON.stringify(me));
        return new Promise<Response>((r) => (resolveAsk = r));
      }),
    );
    await signIn();
    await userEvent.type(screen.getByLabelText(/Ask a question/), "Slow?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/Searching authorized documents/);
    resolveAsk(new Response("{}", { status: 503, headers: { "X-Trace-Id": "b".repeat(32) } }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/unavailable.*trace b{32}/));
  });
});
