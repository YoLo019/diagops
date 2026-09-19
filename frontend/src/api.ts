export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "";

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type InvestigationStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";
export type InvestigationStrategy = "fixed" | "adaptive";
export type AdaptiveRunStatus = "not_applicable" | "completed" | "degraded" | "skipped";
export type AdaptiveStopReason =
  | "sufficient_evidence"
  | "budget_exhausted"
  | "no_new_evidence"
  | "duplicate_query"
  | "round_limit"
  | "timeout"
  | "failed";

export type ActionStatus = "proposed" | "approved" | "rejected" | "skipped" | "done";
export type VerificationStatus = "pending" | "passed" | "failed" | "skipped";
export type AgentExecutionLayer = "custom" | "openai_agents_sdk";
export type AuthorityMode = "agent" | "legacy_deterministic";
export type DiagnosticStatus = "complete" | "partial" | "inconclusive";
export type CoordinationDecisionStatus = "agreement" | "conflict" | "agent_leads" | "fallback";
export type MultiAgentRunStatus = "completed" | "partial" | "failed" | "skipped";
export type ModelProvider = "openai" | "deepseek" | "openai_compatible";
export type ResultValidationCategory =
  | "task_contract"
  | "execution_contract"
  | "finding_contract"
  | "finding_revision_contract"
  | "review_attribution"
  | "review_contract"
  | "semantic_reference"
  | "run_status_contract";
export type ExecutionStepKind =
  | "initial_coordination"
  | "specialist_collection"
  | "specialist_recollection"
  | "final_synthesis"
  | "reference_validation"
  | "hybrid_arbitration"
  | "review_persistence"
  | "result_validation"
  | "lead_planning"
  | "investigator_analysis"
  | "critic_review"
  | "lead_adjudication";
export type FailureCategory =
  | "none"
  | "not_configured"
  | "authentication"
  | "rate_limit"
  | "quota"
  | "timeout"
  | "cancelled"
  | "transport"
  | "invalid_output"
  | "invalid_reference"
  | "missing_specialist"
  | "unsafe_output"
  | "persistence"
  | "contract_integrity"
  | "unknown";
export type StabilizationCategory =
  | "reference_validation"
  | "unsafe_output"
  | "cancelled_or_timeout"
  | "provider_or_sdk_transport"
  | "coordinator_output_contract"
  | "specialist_output_contract"
  | "evidence_cause_mapping"
  | "genuine_conflict"
  | "missing_specialist"
  | "final_synthesis"
  | "review_persistence"
  | "hybrid_contract"
  | "result_validation"
  | "unknown";

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
  related_candidate_ids?: string[];
  runtime_run_id?: string | null;
};

export type VerificationSuggestion = {
  id: string;
  title: string;
  description: string;
  expected_signal: string;
  status: VerificationStatus;
  result_note?: string | null;
  result_evidence_ids: string[];
  related_action_ids: string[];
  related_cause_types: string[];
  related_candidate_ids?: string[];
  runtime_run_id?: string | null;
};

export type IncidentReport = {
  investigation_id: string;
  summary: string;
  timeline: Array<Record<string, string>>;
  hypotheses: Hypothesis[];
  markdown: string;
  action_ids: string[];
  verification_suggestion_ids: string[];
  diagnoses: RootCauseCandidate[];
  alternatives: RootCauseCandidate[];
  diagnostic_status?: DiagnosticStatus | null;
  authority_mode?: AuthorityMode | null;
  critic_assessments: CriticAssessment[];
  critic_summary?: string | null;
  evidence_gaps: string[];
  total_input_tokens: number;
  total_output_tokens: number;
  elapsed_time_ms: number;
  runtime_run_id?: string | null;
};

export type InvestigationRecord = {
  id: string;
  event: IncidentEvent;
  status: InvestigationStatus;
  strategy: InvestigationStrategy;
  multi_agent_run?: MultiAgentRunSummary | null;
  evidence: EvidenceItem[];
  hypotheses: Hypothesis[];
  report?: IncidentReport | null;
  failure_reason?: string | null;
  actions: RecommendedAction[];
  verification_suggestions: VerificationSuggestion[];
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  runtime_available: boolean;
  active_runtime_run_id?: string | null;
  source_investigation_id?: string | null;
};

export type LeadDecision = {
  action: string;
  summary: string;
  task_ids: string[];
  candidate_ids: string[];
  evidence_ids: string[];
  selected_skills: string[];
  stop_reason?: string | null;
};

export type CriticAssessment = {
  id: string;
  candidate_id: string;
  verdict: string;
  checks: Array<{
    name: string;
    status: string;
    summary: string;
    evidence_ids: string[];
    gap?: string | null;
  }>;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  gap?: string | null;
  supplemental_task_ids: string[];
  summary: string;
  runtime_run_id?: string | null;
  review_round: 1 | 2;
};

export type InvestigationSummary = {
  id: string;
  status: string;
  service: string;
  title: string;
  strategy: InvestigationStrategy;
  top_cause_type: string;
  confidence: number;
  top_affected_entity?: string | null;
  top_failure_mechanism?: string | null;
  diagnostic_status?: string | null;
  authority_mode?: string | null;
  lead_decision?: LeadDecision | null;
  final_decision?: {
    actor: "critic" | "lead" | "single";
    action: "conclude" | "inconclusive";
    candidate_ids: string[];
    evidence_ids: string[];
    summary: string;
    stop_reason?: string | null;
    uncertainty?: string | null;
  } | null;
  diagnosis_contract_revision?: 1 | 2;
  critic_assessments?: CriticAssessment[];
  active_runtime_run_id?: string | null;
  action_count: number;
  verification_count: number;
  failure_reason?: string | null;
  runtime_available: boolean;
};

export type RuntimeRunStatus =
  | "created"
  | "running"
  | "cancelling"
  | "interrupted"
  | "completed"
  | "failed"
  | "cancelled";

export type RuntimePhase =
  | "intake"
  | "evidence_collection"
  | "deterministic_rca"
  | "specialist_analysis"
  | "conflict_review"
  | "coordination"
  | "report_generation"
  | "lead_planning"
  | "investigator_round_1"
  | "critic_review"
  | "investigator_round_2"
  | "critic_reconciliation"
  | "lead_adjudication"
  | "result_validation"
  | "finalize";

export type RuntimeRun = {
  id: string;
  investigation_id: string;
  run_kind: "live" | "replay";
  strategy: InvestigationStrategy;
  status: RuntimeRunStatus;
  current_phase?: RuntimePhase | null;
  source_run_id?: string | null;
  parent_run_id?: string | null;
  run_reason: "initial" | "additional_evidence" | "manual_rerun" | "replay";
  model_provider?: ModelProvider | null;
  model_name?: string | null;
  prompt_version?: string | null;
  failure_category?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  cancel_requested_at?: string | null;
  execution_contract_version?: "v10_legacy" | "v11";
  authority_mode?: AuthorityMode;
};

export type RuntimeAttempt = {
  id: string;
  run_id: string;
  attempt_number: number;
  resume_from_checkpoint_id?: string | null;
  status: string;
  started_at: string;
  completed_at?: string | null;
  failure_category?: string | null;
};

export type RuntimeEvent = {
  id: string;
  run_id: string;
  attempt_id: string;
  sequence: number;
  event_type: string;
  phase?: RuntimePhase | null;
  actor_type: string;
  actor_name?: string | null;
  task_id?: string | null;
  execution_id?: string | null;
  tool_call_id?: string | null;
  evidence_ids: string[];
  safe_payload: Record<string, JsonValue>;
  occurred_at: string;
  schema_version: number;
};

export type RuntimeCheckpoint = {
  id: string;
  run_id: string;
  attempt_id: string;
  completed_phase: RuntimePhase;
  event_sequence: number;
  state_digest: string;
  projection_digest: string;
  resume_state: Record<string, JsonValue>;
  created_at: string;
  schema_version: number;
};

export type RuntimeRunDetail = RuntimeRun & {
  attempts: RuntimeAttempt[];
  checkpoints: RuntimeCheckpoint[];
};

export type ReplayReport = {
  id: string;
  replay_run_id: string;
  source_run_id: string;
  valid: boolean;
  validation_errors: string[];
  benchmark_evaluation: string;
  external_call_count: number;
  created_at: string;
};

export type RuntimeDiffSection = {
  left: JsonValue;
  right: JsonValue;
  changed: boolean;
};

export type RuntimeRunDiff = {
  run_id: string;
  against_run_id: string;
  sections: Record<string, RuntimeDiffSection>;
};

export type RuntimeRunCreatePayload = {
  strategy: InvestigationStrategy;
  run_reason: "initial" | "additional_evidence" | "manual_rerun";
  parent_run_id?: string | null;
  model_provider?: ModelProvider | null;
  model_name?: string | null;
  prompt_version?: string | null;
  execution_contract_version?: "v10_legacy" | "v11";
};

export type ManualInvestigationPayload = {
  text: string;
  service: string;
  environment: string;
  strategy: InvestigationStrategy;
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
  execution_layer?: AgentExecutionLayer;
  analysis_round?: 1 | 2 | null;
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
  execution_layer?: AgentExecutionLayer;
  analysis_round?: 1 | 2 | null;
  step_kind?: ExecutionStepKind | null;
  attempt?: number;
  failure_category?: FailureCategory;
  result_validation_category?: ResultValidationCategory | null;
  model_provider?: ModelProvider | null;
  model_name?: string | null;
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
  blocking: boolean;
  execution_layer?: AgentExecutionLayer;
  analysis_round?: 1 | 2;
  runtime_run_id?: string | null;
  revises_finding_id?: string | null;
  created_at: string;
};

export type RootCauseCandidate = {
  id: string;
  cause_type?: string | null;
  affected_entity?: string | null;
  failure_mechanism?: string | null;
  summary: string;
  rank: number;
  confidence: number;
  supporting_finding_ids: string[];
  contradicting_finding_ids: string[];
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  rationale: string;
  uncertainty: string;
  onset_window_start?: string | null;
  onset_window_end?: string | null;
};

export type CoordinationReview = {
  id: string;
  investigation_id: string;
  candidates: RootCauseCandidate[];
  execution_layer?: AgentExecutionLayer;
  authority_mode?: AuthorityMode;
  runtime_run_id?: string | null;
  diagnostic_status?: DiagnosticStatus | null;
  stop_reason?: string | null;
  lead_decision?: LeadDecision | null;
  final_decision?: {
    actor: "critic" | "lead" | "single";
    action: "conclude" | "inconclusive";
    candidate_ids: string[];
    evidence_ids: string[];
    summary: string;
    stop_reason?: string | null;
    uncertainty?: string | null;
  } | null;
  diagnosis_contract_revision?: 1 | 2;
  critic_assessments?: CriticAssessment[];
  run_status?: MultiAgentRunStatus;
  decision_status?: CoordinationDecisionStatus | null;
  baseline_cause_type?: string | null;
  selected_cause_type?: string | null;
  summary?: string;
  uncertainty?: string;
  model_provider?: ModelProvider | null;
  model_name?: string | null;
  primary_stabilization_category?: StabilizationCategory | null;
  secondary_stabilization_categories?: StabilizationCategory[];
  created_at: string;
};

export type MultiAgentRunSummary = {
  status: MultiAgentRunStatus;
  failure_reason?: string | null;
  model_provider?: ModelProvider | null;
  model_name?: string | null;
  primary_stabilization_category?: StabilizationCategory | null;
  secondary_stabilization_categories?: StabilizationCategory[];
  strategy?: InvestigationStrategy;
  adaptive_status?: AdaptiveRunStatus;
  adaptive_stop_reason?: AdaptiveStopReason | null;
  tool_call_count?: number;
  max_tool_calls_per_specialist?: number;
  max_total_tool_calls?: number;
  diagnostic_status?: DiagnosticStatus | null;
  authority_mode?: AuthorityMode | null;
  completed_rounds?: number;
  investigator_count?: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
  elapsed_time_ms?: number;
  runtime_run_id?: string | null;
};

export type AgentConfig = {
  provider: ModelProvider;
  model?: string | null;
  implementation_status: "implemented" | "unsupported";
  certification_status: "certified" | "failed" | "not_run";
  strategy: InvestigationStrategy;
  max_tool_calls_per_specialist: number;
  max_total_tool_calls: number;
  tool_timeout_seconds: number;
};

export type RcaWorkbench = {
  investigation: InvestigationRecord;
  findings: AgentFinding[];
  candidates: RootCauseCandidate[];
  evidence: EvidenceItem[];
  coordination_review?: CoordinationReview | null;
  agent_executions?: AgentExecution[];
  tool_calls: ToolCallRecord[];
  multi_agent_run?: MultiAgentRunSummary | null;
  agent_config?: AgentConfig | null;
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

export type OpenRcaStrategySummary = {
  case_count: number;
  completed_count: number;
  completion_rate: number;
  evidence_reference_validity: number;
  invalid_evidence_references: number;
  read_only_violations: number;
  average_tool_calls: number;
  duplicate_query_rejections: number;
  average_duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  strict_accuracy?: number | null;
  partial_score?: number | null;
  component_score?: number | null;
  reason_score?: number | null;
  time_score?: number | null;
  per_partition: Record<string, { strict_accuracy: number; partial_score: number }>;
  failed_cases: Array<{ case_id: string; category: string }>;
};

export type OpenRcaBenchmarkSummary = {
  run_id: string;
  case_count: number;
  model: string;
  prompt_version: string;
  git_commit: string;
  started_at: string;
  completed_at: string;
  strategies: Record<string, OpenRcaStrategySummary>;
};

export type OpenRcaArtifactName =
  | "run-manifest.json"
  | "fixed-predictions.csv"
  | "adaptive-predictions.csv"
  | "v11-agent-predictions.csv"
  | "official-report.csv"
  | "summary.json";

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
      // 响应体不是 JSON 时保留 HTTP 状态文本。
    }
    throw new Error(detail);
  }

  return response.json() as Promise<T>;
}

export function listInvestigations() {
  return request<InvestigationSummary[]>("/investigations/summaries");
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

export function getRcaWorkbench(id: string) {
  return request<RcaWorkbench>(`/investigations/${id}/rca-workbench`);
}

export function getLatestOpenRcaBenchmark() {
  return request<OpenRcaBenchmarkSummary>("/benchmarks/openrca/latest");
}

export function openRcaArtifactUrl(name: OpenRcaArtifactName) {
  return `${API_BASE_URL}/benchmarks/openrca/latest/${encodeURIComponent(name)}`;
}

export function createManualInvestigation(payload: ManualInvestigationPayload) {
  return request<InvestigationSummary>("/investigations/manual", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createRuntimeRun(
  investigationId: string,
  payload: RuntimeRunCreatePayload,
) {
  return request<RuntimeRun>(`/investigations/${investigationId}/runtime-runs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listRuntimeRuns(investigationId: string) {
  return request<RuntimeRun[]>(`/investigations/${investigationId}/runtime-runs`);
}

export function getRuntimeRun(runId: string) {
  return request<RuntimeRunDetail>(`/runtime-runs/${runId}`);
}

export function getRuntimeEvents(runId: string, after = 0, limit = 200) {
  return request<RuntimeEvent[]>(
    `/runtime-runs/${runId}/events?after=${after}&limit=${limit}`,
  );
}

export function cancelRuntimeRun(runId: string) {
  return request<RuntimeRun>(`/runtime-runs/${runId}/cancel`, { method: "POST" });
}

export function resumeRuntimeRun(runId: string) {
  return request<RuntimeRun>(`/runtime-runs/${runId}/resume`, { method: "POST" });
}

export function replayRuntimeRun(runId: string) {
  return request<ReplayReport>(`/runtime-runs/${runId}/replay`, { method: "POST" });
}

export function diffRuntimeRuns(runId: string, againstRunId: string) {
  return request<RuntimeRunDiff>(
    `/runtime-runs/${runId}/diff?against_run_id=${encodeURIComponent(againstRunId)}`,
  );
}

export function runtimeEventStreamUrl(runId: string, lastSequence: number) {
  const path = `/runtime-runs/${encodeURIComponent(runId)}/events/stream?last_event_id=${lastSequence}`;
  return `${API_BASE_URL}${path}`;
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
  resultEvidenceIds: string[],
  relatedActionIds: string[],
  relatedCauseTypes: string[],
  relatedCandidateIds: string[] = [],
) {
  return request<VerificationSuggestion>(
    `/investigations/${investigationId}/verifications/${verificationId}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        status,
        result_note: resultNote || null,
        result_evidence_ids: resultEvidenceIds,
        related_action_ids: relatedActionIds,
        related_cause_types: relatedCauseTypes,
        related_candidate_ids: relatedCandidateIds,
      }),
    },
  );
}
