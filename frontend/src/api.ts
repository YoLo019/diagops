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

export type DiagnosisTask = {
  id: string;
  title: string;
  description: string;
  task_type: string;
  agent_name: string;
  tool_names: string[];
  depends_on: string[];
  priority: number;
  status: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
};

export type DiagnosisPlan = {
  id: string;
  investigation_id: string;
  tasks: DiagnosisTask[];
  created_at: string;
};

export type AgentExecution = {
  id: string;
  task_id: string;
  agent_name: string;
  status: string;
  tool_call_ids: string[];
  evidence_ids: string[];
  summary?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms: number;
};

export type ContextFact = {
  id: string;
  source_agent: string;
  fact_type: string;
  summary: string;
  confidence: number;
  evidence_ids: string[];
  created_at: string;
};

export type ToolCallRecord = {
  id: string;
  task_id: string;
  agent_name: string;
  tool_name: string;
  input: Record<string, unknown>;
  status: string;
  output_evidence_ids: string[];
  error_message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms: number;
};

export type MemoryItem = {
  id: string;
  service: string;
  environment: string;
  memory_type: string;
  summary: string;
  source_investigation_id?: string | null;
  tags: string[];
  created_at: string;
};

export type TaskGraph = {
  nodes: Array<{
    id: string;
    label: string;
    title: string;
    type: string;
    status: string;
    agent_name: string;
  }>;
  edges: Array<{
    source: string;
    target: string;
  }>;
};

export type AgentFinding = {
  id: string;
  investigation_id: string;
  agent_name: string;
  finding_type: string;
  summary: string;
  confidence: number;
  evidence_ids: string[];
  related_cause_type?: string | null;
  severity: string;
  rationale: string;
  gaps: string[];
  created_at: string;
};

export type RootCauseCandidate = {
  id: string;
  cause_type: string;
  summary: string;
  rank: number;
  confidence: number;
  supporting_finding_ids: string[];
  contradicting_finding_ids: string[];
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  rationale: string;
  uncertainty: string;
};

export type CoordinationReview = {
  id: string;
  investigation_id: string;
  candidates: RootCauseCandidate[];
  created_at: string;
};

export type RcaWorkbench = {
  investigation: InvestigationRecord;
  findings: AgentFinding[];
  candidates: RootCauseCandidate[];
  evidence: EvidenceItem[];
  graph_seed: {
    nodes: Array<{
      id: string;
      label: string;
      type: string;
    }>;
    edges: Array<{
      source: string;
      relation: string;
      target: string;
    }>;
  };
};

export type ReActTraceStep = {
  step_number: number;
  assistant_text?: string | null;
  tool_name?: string | null;
  tool_input: Record<string, unknown>;
  tool_call_id?: string | null;
  observation?: string | null;
  output_evidence_ids: string[];
  status: string;
  error_message?: string | null;
  started_at: string;
  completed_at?: string | null;
};

export type ReActTrace = {
  id: string;
  investigation_id: string;
  status: string;
  final_answer?: string | null;
  steps: ReActTraceStep[];
  created_at: string;
  completed_at?: string | null;
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

export function getPlan(id: string) {
  return request<DiagnosisPlan | null>(`/investigations/${id}/plan`);
}

export function getTasks(id: string) {
  return request<DiagnosisTask[]>(`/investigations/${id}/tasks`);
}

export function getAgentExecutions(id: string) {
  return request<AgentExecution[]>(`/investigations/${id}/agent-executions`);
}

export function getContextFacts(id: string) {
  return request<ContextFact[]>(`/investigations/${id}/context`);
}

export function getToolCalls(id: string) {
  return request<ToolCallRecord[]>(`/investigations/${id}/tool-calls`);
}

export function getMemoryHits(id: string) {
  return request<MemoryItem[]>(`/investigations/${id}/memory`);
}

export function getTaskGraph(id: string) {
  return request<TaskGraph>(`/investigations/${id}/task-graph`);
}

export function getAgentFindings(id: string) {
  return request<AgentFinding[]>(`/investigations/${id}/agent-findings`);
}

export function getCoordinationReview(id: string) {
  return request<CoordinationReview | null>(`/investigations/${id}/coordination-review`);
}

export function getRcaWorkbench(id: string) {
  return request<RcaWorkbench>(`/investigations/${id}/rca-workbench`);
}

export function getReActTrace(id: string) {
  return request<ReActTrace | null>(`/investigations/${id}/react-trace`);
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
