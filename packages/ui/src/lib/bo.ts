// Typed client for the /bo surface (BOAgents Valul 1). Errors carry the HTTP
// status and the machine-readable `error` code so pages can render the
// required states (forbidden, conflict, invalid, not_found) instead of a
// generic failure.

const BO_BASE = "/api/backend/bo";

export class BoApiError extends Error {
  status: number;
  code: string;
  detail?: unknown;

  constructor(status: number, code: string, detail?: unknown) {
    super(`${status} ${code}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function req<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${BO_BASE}${path}`, init);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const code =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : "http_error";
    throw new BoApiError(res.status, code, (body as { detail?: unknown })?.detail);
  }
  return body as T;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface BoSetting {
  key: string;
  schema_version: string;
  type: "text" | "enum" | "integer" | "timezone";
  default: unknown;
  apply_mode: "IMMEDIATE" | "NEW_RUN" | "RESTART" | "MIGRATION";
  scope: string;
  page: string;
  tab: string;
  label_ro: string;
  label_en: string;
  help_ro: string;
  edit_role: string;
  sensitivity: string;
  effect_ro: string;
  value: unknown;
  origin: "tenant" | "default";
  version: number;
  updated_by: string | null;
  updated_at: string | null;
}

export interface BoSettingsResponse {
  tenant: string;
  config_version: number;
  settings: BoSetting[];
  role: "admin" | "operator" | "viewer";
}

export function getBoSettings(): Promise<BoSettingsResponse> {
  return req("/settings");
}

export function putBoSetting(
  key: string,
  value: unknown,
  expectedVersion: number,
): Promise<{ result: string; applied: boolean; setting: BoSetting }> {
  return req(`/settings/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, expected_version: expectedVersion }),
  });
}

// ---------------------------------------------------------------------------
// BoBots
// ---------------------------------------------------------------------------

export interface BoBot {
  id: string;
  tenant: string;
  name: string;
  description: string;
  kind: "BOT" | "AI" | "MIXED";
  owner: string;
  scope: string;
  status: "draft" | "active";
  draft_version: number;
  active_version_no: number | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface BoBotVersion {
  id: string;
  definition_id: string;
  version_no: number;
  schema_version: string;
  hash: string;
  status: "draft" | "published";
  created_by: string;
  created_at: string;
  content?: Record<string, unknown>;
}

export interface BoBotDetail {
  bot: BoBot;
  versions: BoBotVersion[];
  draft: BoBotVersion;
  active: BoBotVersion | null;
}

export interface BoStepRun {
  idx: number;
  step_id: string;
  step_type: string;
  status: "SIMULATED" | "SKIPPED" | string;
  ts: string;
  detail: Record<string, unknown> | null;
  receipt: { effect_status?: string; note?: string } | null;
}

export interface BoRunResult {
  execution_status: "SUCCEEDED" | "PARTIAL" | "FAILED" | string;
  predicate_result: "TRUE" | "FALSE" | "UNKNOWN" | "NOT_EVALUATED";
  gated_by_predicate: boolean;
  step_count: number;
  steps_evaluated: number;
  step_limit: number;
  capped_by_step_limit: boolean;
  findings: {
    finding_key: string;
    category: string;
    severity: string;
    message: string;
    effect_status: string;
  }[];
  simulation: boolean;
  external_effects: number;
}

export interface BoRun {
  id: string;
  tenant: string;
  definition_id: string;
  version_id: string;
  version_no: number;
  version_hash: string;
  kind: string;
  status: string;
  input: Record<string, unknown> | null;
  config_version: number;
  config_snapshot: Record<string, unknown> | null;
  plan_hash: string;
  result: BoRunResult | null;
  error: string | null;
  created_by: string;
  started_at: string;
  finished_at: string | null;
  timeline?: BoStepRun[];
}

export function listBoBots(): Promise<{ bots: BoBot[] }> {
  return req("/bots");
}

export function getBoBot(id: string): Promise<BoBotDetail> {
  return req(`/bots/${id}`);
}

export function createBoBot(payload: {
  name: string;
  kind?: string;
  description?: string;
  content: Record<string, unknown>;
}): Promise<{ bot: BoBot }> {
  return req("/bots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function createExampleBoBot(): Promise<{ bot: BoBot }> {
  return req("/bots/example", { method: "POST" });
}

export function patchBoBot(
  id: string,
  body: {
    expected_version: number;
    name?: string;
    description?: string;
    content?: Record<string, unknown>;
  },
): Promise<{ bot: BoBot }> {
  return req(`/bots/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function publishBoBot(
  id: string,
): Promise<{ bot: BoBot; result: string; active_version_no: number }> {
  return req(`/bots/${id}/publish`, { method: "POST" });
}

export function simulateBoBot(
  id: string,
  body: { input?: Record<string, unknown>; version_no?: number; draft?: boolean },
): Promise<{ run: BoRun }> {
  return req(`/bots/${id}/simulate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listBoRuns(id: string): Promise<{ runs: BoRun[] }> {
  return req(`/bots/${id}/runs`);
}

export function getBoRun(runId: string): Promise<{ run: BoRun }> {
  return req(`/runs/${runId}`);
}

export interface BoTelemetryStatus {
  enabled: boolean;
  transport: string;
  emitted: number;
  dropped: number;
  rejected: number;
  schema_version: string;
  note: string;
}

export function getBoTelemetryStatus(): Promise<BoTelemetryStatus> {
  return req("/telemetry/status");
}
