export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "";

export type InvestigationStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type ActionStatus = "proposed" | "approved" | "rejected" | "skipped" | "done";
export type VerificationStatus = "pending" | "passed" | "failed" | "skipped";

export type IncidentEvent = {
  source: string;
  service: string;
  environment: string;
  severity: string;
  title: string;
  description: string;
  started_at: string;
};

export type EvidenceItem = {
  id: string;
  provider: string;
  kind: string;
  timestamp: string;
  summary: string;
  payload: Record<string, unknown>;
  confidence: number;
  status: string;
  error_message?: string | null;
};

export type Hypothesis = {
  id: string;
  cause_type: string;
  summary: string;
  confidence: number;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  next_actions: string[];
};

export type RecommendedAction = {
  id: string;
  action_type: string;
  title: string;
  description: string;
  risk_level: string;
  requires_approval: boolean;
  supporting_evidence_ids: string[];
  status: ActionStatus;
  note?: string | null;
};

export type VerificationSuggestion = {
  id: string;
  title: string;
  description: string;
  expected_signal: string;
  status: VerificationStatus;
  result_note?: string | null;
};

export type IncidentReport = {
  investigation_id: string;
  summary: string;
  timeline: Array<Record<string, string>>;
  hypotheses: Hypothesis[];
  markdown: string;
  action_ids: string[];
  verification_suggestion_ids: string[];
};

export type InvestigationRecord = {
  id: string;
  event: IncidentEvent;
  status: InvestigationStatus;
  evidence: EvidenceItem[];
  hypotheses: Hypothesis[];
  report?: IncidentReport | null;
  failure_reason?: string | null;
  actions: RecommendedAction[];
  verification_suggestions: VerificationSuggestion[];
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
};

export type InvestigationSummary = {
  id: string;
  status: string;
  service: string;
  title: string;
  top_cause_type: string;
  confidence: number;
  action_count: number;
  verification_count: number;
  failure_reason?: string | null;
};

export type ManualInvestigationPayload = {
  text: string;
  service: string;
  environment: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    ...init,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // Keep the HTTP status text when the body is not JSON.
    }
    throw new Error(detail);
  }

  return response.json() as Promise<T>;
}

export function listInvestigations() {
  return request<InvestigationRecord[]>("/investigations");
}

export function getInvestigation(id: string) {
  return request<InvestigationRecord>(`/investigations/${id}`);
}

export function createManualInvestigation(payload: ManualInvestigationPayload) {
  return request<InvestigationSummary>("/investigations/manual", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateActionStatus(
  investigationId: string,
  actionId: string,
  status: ActionStatus,
  note: string,
) {
  return request<RecommendedAction>(`/investigations/${investigationId}/actions/${actionId}`, {
    method: "PATCH",
    body: JSON.stringify({ status, note: note || null }),
  });
}

export function updateVerificationStatus(
  investigationId: string,
  verificationId: string,
  status: VerificationStatus,
  resultNote: string,
) {
  return request<VerificationSuggestion>(
    `/investigations/${investigationId}/verifications/${verificationId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ status, result_note: resultNote || null }),
    },
  );
}
