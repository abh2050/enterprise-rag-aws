// Typed API client. The browser only ever sends a bearer access token and the question.
// Tenant, groups, clearance and filters are derived server-side and are never sent from here.

export type AnswerStatus = "answered" | "limited" | "abstained";
export type ResponseMode = "standard" | "high_assurance";

export interface Citation {
  citation_id: string;
  evidence_id: string;
  document_id: string;
  document_version: string;
  title: string;
  location: string;
  section: string;
  source_uri: string;
  open_url: string;
  source_modified_at: string | null;
  acl_synced_at: string | null;
  is_latest_revision: boolean;
}

export interface AnswerClaim {
  text: string;
  citation_ids: string[];
}

export interface AnswerResponse {
  run_id: string;
  trace_id: string;
  conversation_id: string;
  status: AnswerStatus;
  mode: ResponseMode;
  claims: AnswerClaim[];
  citations: Citation[];
  conflicts: string[];
  outdated_sources: string[];
  abstain_reason: string | null;
  judge_status: string;
  from_cache: boolean;
  inference_mode: "fixture" | "live";
}

export interface CitationDetail {
  citation_id: string;
  document_id: string;
  document_version: string;
  title: string;
  section: string;
  location: string;
  text: string;
  source_uri: string;
  source_modified_at: string | null;
  acl_synced_at: string | null;
  sensitivity_label: string;
  download_url: string;
}

export interface Me {
  display_name: string | null;
  tenant_id: string;
  clearance: string;
  projects: string[];
  is_admin: boolean;
  group_count: number;
  auth_provider: string;
  inference_mode: "fixture" | "live";
}

export interface DevUser {
  username: string;
  display_name: string;
  description: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly traceId: string | null,
  ) {
    super(message);
  }
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export type TokenProvider = () => Promise<string>;

async function request<T>(path: string, getToken: TokenProvider | null, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (getToken) headers.set("Authorization", `Bearer ${await getToken()}`);
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!resp.ok) {
    const traceId = resp.headers.get("X-Trace-Id");
    const message =
      resp.status === 401
        ? "Your session is not valid. Please sign in again."
        : resp.status === 404
          ? "Not found, or you do not have access."
          : resp.status === 503
            ? "A required service is unavailable. Please try again shortly."
            : `Request failed (${resp.status}).`;
    throw new ApiError(resp.status, message, traceId);
  }
  return (await resp.json()) as T;
}

export const api = {
  devUsers: () => request<{ synthetic: boolean; users: DevUser[] }>("/dev-idp/users", null),
  devToken: (username: string) =>
    request<{ access_token: string }>("/dev-idp/token", null, {
      method: "POST",
      body: JSON.stringify({ username }),
    }),
  me: (t: TokenProvider) => request<Me>("/api/me", t),
  ask: (t: TokenProvider, question: string, mode: ResponseMode, conversationId: string | null) =>
    request<AnswerResponse>("/api/ask", t, {
      method: "POST",
      body: JSON.stringify({ question, mode, ...(conversationId ? { conversation_id: conversationId } : {}) }),
    }),
  citation: (t: TokenProvider, openUrl: string) => request<CitationDetail>(openUrl, t),
  download: async (t: TokenProvider, url: string): Promise<Blob> => {
    const resp = await fetch(`${BASE}${url}`, { headers: { Authorization: `Bearer ${await t()}` } });
    if (!resp.ok) throw new ApiError(resp.status, "Not found, or you do not have access.", resp.headers.get("X-Trace-Id"));
    return resp.blob();
  },
  feedback: (t: TokenProvider, runId: string, rating: "up" | "down", reason: string | null, comment: string | null) =>
    request<{ status: string }>("/api/feedback", t, {
      method: "POST",
      body: JSON.stringify({ run_id: runId, rating, reason, comment }),
    }),
};
