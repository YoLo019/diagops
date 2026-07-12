import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  API_BASE_URL,
  createManualInvestigation,
  getAgentExecutions,
  getContextFacts,
  getInvestigation,
  getMemoryHits,
  getPlan,
  getRcaWorkbench,
  getReActTrace,
  getTaskGraph,
  getTasks,
  getToolCalls,
  listInvestigations,
  updateActionStatus,
  updateVerificationStatus,
  type ActionStatus,
  type AgentExecutionLayer,
  type AgentExecution,
  type AgentFinding,
  type ContextFact,
  type CoordinationDecisionStatus,
  type CoordinationReview,
  type DiagnosisPlan,
  type DiagnosisTask,
  type InvestigationRecord,
  type MemoryItem,
  type MultiAgentRunSummary,
  type RcaWorkbench,
  type ReActTrace,
  type RootCauseCandidate,
  type TaskGraph,
  type ToolCallRecord,
  type VerificationStatus,
} from "./api";

const actionStatuses: ActionStatus[] = ["proposed", "approved", "rejected", "skipped", "done"];
const verificationStatuses: VerificationStatus[] = ["pending", "passed", "failed", "skipped"];
const statusLabels: Record<string, string> = {
  approved: "已批准",
  cancelled: "已取消",
  completed: "已完成",
  done: "已完成",
  failed: "失败",
  partial: "部分完成",
  passed: "已通过",
  pending: "待处理",
  proposed: "待审批",
  rejected: "已驳回",
  running: "诊断中",
  skipped: "已跳过",
  success: "成功",
};
const decisionLabels: Record<CoordinationDecisionStatus, string> = {
  agreement: "多 Agent 复核一致",
  conflict: "存在冲突，需要人工确认",
  agent_leads: "多 Agent 主要候选，尚未确认",
  fallback: "多 Agent 复核未完成，以下为确定性 RCA 结果",
};

export function selectDisplayedCause(
  decisionStatus: CoordinationDecisionStatus,
  selectedCause: string | null | undefined,
  baselineCause: string,
) {
  if (decisionStatus === "conflict") {
    return "未选择（冲突待人工确认）";
  }
  if (decisionStatus === "fallback") {
    return baselineCause;
  }
  if (selectedCause) {
    return selectedCause;
  }
  return "未选择";
}

export function isUsableV7Review(
  review: CoordinationReview | null | undefined,
  run: MultiAgentRunSummary | null | undefined,
): review is CoordinationReview {
  if (
    review?.execution_layer !== "openai_agents_sdk" ||
    !run ||
    !["completed", "partial"].includes(run.status) ||
    review.run_status !== run.status ||
    !review.decision_status
  ) {
    return false;
  }
  if (review.decision_status === "conflict") {
    return review.selected_cause_type === null;
  }
  if (review.decision_status === "agreement") {
    return (
      run.status === "completed" &&
      Boolean(review.baseline_cause_type) &&
      review.selected_cause_type === review.baseline_cause_type
    );
  }
  if (review.decision_status === "agent_leads") {
    return review.selected_cause_type !== null && review.selected_cause_type !== undefined;
  }
  return true;
}

export function selectVisibleCandidates(
  review: CoordinationReview | null | undefined,
  run: MultiAgentRunSummary | null | undefined,
  legacyCandidates: RootCauseCandidate[],
) {
  if (review?.execution_layer !== "openai_agents_sdk") {
    return legacyCandidates;
  }
  return isUsableV7Review(review, run) ? review.candidates : [];
}

export function projectAgentUiText(text: string, executionLayer?: AgentExecutionLayer) {
  if (executionLayer !== "openai_agents_sdk") {
    return text;
  }
  if (
    /已执行|已修复|修复成功|回滚|重启|扩容|缩容|配置.*修改|修复.*完成|配置变更|\b(?:i|we)\s+(?:successfully\s+)?updated\s+(?:the\s+)?(?:production\s+)?config(?:uration)?(?:\s+successfully)?\b|\b(?:i|we)\s+(?:already\s+)?rolled\s+back\s+(?:the\s+)?(?:deployment|service|release)\b|\b(?:i|we)\s+(?:already\s+)?applied\s+(?:the\s+)?config(?:uration)?\s+(?:change|update)\b|\b(?:ssh|rollback|restart|scale|rebooted|repaired|fixed|executed)\b/isu.test(
      text,
    )
  ) {
    return "[未验证操作声明已省略]";
  }

  return text
    .replace(/\bbearer\s+[^\s,;]+/giu, "[REDACTED]")
    .replace(
      /\b(?:[\w-]*(?:token|secret|password|passwd|pwd)|(?:api|access|private)[_-]?key|key)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/giu,
      "[REDACTED]",
    )
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@[^\s]+/giu, "[REDACTED_URL]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/giu, "[REDACTED_EMAIL]")
    .trim()
    .replace(/\s+/gu, " ");
}

function formatDate(value?: string | null) {
  if (!value) {
    return "暂无";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function formatIdList(values: string[]) {
  return values.length > 0 ? values.join(", ") : "无";
}

function StatusBadge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{statusLabels[value] ?? value}</span>;
}

function MarkdownReport({ markdown }: { markdown: string }) {
  const lines = markdown.split("\n");

  return (
    <div className="markdown-report">
      {lines.map((line, index) => {
        const key = `${index}-${line}`;
        if (line.startsWith("### ")) {
          return <h4 key={key}>{line.slice(4)}</h4>;
        }
        if (line.startsWith("## ")) {
          return <h3 key={key}>{line.slice(3)}</h3>;
        }
        if (line.startsWith("# ")) {
          return <h2 key={key}>{line.slice(2)}</h2>;
        }
        if (line.startsWith("- ")) {
          return <p key={key} className="markdown-list-item">{line.slice(2)}</p>;
        }
        if (!line.trim()) {
          return <div key={key} className="markdown-break" />;
        }
        return <p key={key}>{line}</p>;
      })}
    </div>
  );
}

function ManualInvestigationForm({ onCreated }: { onCreated: (id: string) => void }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [service, setService] = useState("checkout-service");
  const [environment, setEnvironment] = useState("prod");

  const mutation = useMutation({
    mutationFn: createManualInvestigation,
    onSuccess: (summary) => {
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
      onCreated(summary.id);
      setText("");
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    mutation.mutate({ text, service, environment });
  }

  return (
    <form className="panel manual-form" onSubmit={submit}>
      <div className="panel-heading">
        <h2>新建诊断</h2>
      </div>
      <label>
        事件描述
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例如：checkout-service 发布后错误率升高"
          required
        />
      </label>
      <div className="field-row">
        <label>
          服务
          <input value={service} onChange={(event) => setService(event.target.value)} required />
        </label>
        <label>
          环境
          <input
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            required
          />
        </label>
      </div>
      {mutation.isError ? <p className="error">{mutation.error.message}</p> : null}
      <button type="submit" disabled={mutation.isPending || !text.trim()}>
        {mutation.isPending ? "创建中..." : "创建诊断"}
      </button>
    </form>
  );
}

function InvestigationList({
  investigations,
  selectedId,
  onSelect,
}: {
  investigations: InvestigationRecord[];
  selectedId?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="panel list-panel">
      <div className="panel-heading">
        <h2>诊断列表</h2>
        <span>{investigations.length}</span>
      </div>
      <div className="investigation-list">
        {investigations.map((item) => {
          const topHypothesis = item.hypotheses[0];
          return (
            <button
              className={item.id === selectedId ? "investigation-item selected" : "investigation-item"}
              key={item.id}
              onClick={() => onSelect(item.id)}
              type="button"
            >
              <span className="item-title">{item.event.title}</span>
              <span className="item-meta">
                {item.event.service} / {item.event.environment}
              </span>
              <span className="item-footer">
                <StatusBadge value={item.status} />
                <span>{topHypothesis ? formatPercent(topHypothesis.confidence) : "0%"}</span>
              </span>
            </button>
          );
        })}
        {investigations.length === 0 ? (
          <div className="empty-state">暂无诊断记录。</div>
        ) : null}
      </div>
    </section>
  );
}

function InvestigationDetail({ investigation }: { investigation: InvestigationRecord }) {
  const topHypothesis = investigation.hypotheses[0];

  return (
    <section className="panel detail-panel">
      <div className="panel-heading">
        <h2>诊断详情</h2>
        <StatusBadge value={investigation.status} />
      </div>
      <div className="detail-grid">
        <div>
          <span className="label">服务</span>
          <strong>{investigation.event.service}</strong>
        </div>
        <div>
          <span className="label">环境</span>
          <strong>{investigation.event.environment}</strong>
        </div>
        <div>
          <span className="label">严重级别</span>
          <strong>{investigation.event.severity}</strong>
        </div>
        <div>
          <span className="label">开始时间</span>
          <strong>{formatDate(investigation.event.started_at)}</strong>
        </div>
      </div>
      <h3>{investigation.event.title}</h3>
      <p>{investigation.event.description}</p>
      {topHypothesis ? (
        <div className="callout">
          <span className="label">首选根因</span>
          <strong>{topHypothesis.cause_type}</strong>
          <span>置信度 {formatPercent(topHypothesis.confidence)}</span>
        </div>
      ) : null}
      {investigation.failure_reason ? <p className="error">{investigation.failure_reason}</p> : null}
    </section>
  );
}

function EvidenceList({ investigation }: { investigation: InvestigationRecord }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>证据链</h2>
        <span>{investigation.evidence.length}</span>
      </div>
      <div className="evidence-list">
        {investigation.evidence.map((item) => (
          <article className="evidence-item" key={item.id}>
            <div>
              <StatusBadge value={item.status} />
              <span className="provider">{item.provider}</span>
              <span className="timestamp">{formatDate(item.timestamp)}</span>
            </div>
            <strong>{item.summary}</strong>
            <p>
              {item.kind} / 置信度 {formatPercent(item.confidence)}
            </p>
            {item.error_message ? <p className="error">{item.error_message}</p> : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function Hypotheses({ investigation }: { investigation: InvestigationRecord }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>候选根因</h2>
        <span>{investigation.hypotheses.length}</span>
      </div>
      <div className="hypothesis-list">
        {investigation.hypotheses.map((hypothesis) => (
          <article className="hypothesis-item" key={hypothesis.id}>
            <div className="score-row">
              <strong>{hypothesis.cause_type}</strong>
              <span>{formatPercent(hypothesis.confidence)}</span>
            </div>
            <p>{hypothesis.summary}</p>
            {hypothesis.next_actions.length > 0 ? (
              <p className="muted">下一步: {hypothesis.next_actions.join(", ")}</p>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function groupFindingsByAgent(findings: AgentFinding[]) {
  return findings.reduce<Record<string, AgentFinding[]>>((groups, finding) => {
    groups[finding.agent_name] = [...(groups[finding.agent_name] ?? []), finding];
    return groups;
  }, {});
}

function AgentFindingList({ findingsByAgent }: { findingsByAgent: Record<string, AgentFinding[]> }) {
  return (
    <div className="compact-list">
      {Object.entries(findingsByAgent).map(([agentName, findings]) => (
        <article className="compact-row" key={agentName}>
          <strong>{agentName}</strong>
          {findings.map((finding) => (
            <div key={finding.id}>
              <p>{projectAgentUiText(finding.summary, finding.execution_layer)}</p>
              <div className="process-detail">
                {finding.execution_layer === "openai_agents_sdk" ? (
                  <>
                    <span>{finding.analysis_round === 2 ? "第 2 轮修订" : "第 1 轮"}</span>
                    {finding.revises_finding_id ? (
                      <span>修订自: {finding.revises_finding_id}</span>
                    ) : null}
                  </>
                ) : null}
                <span>类型: {finding.finding_type}</span>
                <span>关联根因: {finding.related_cause_type ?? "无"}</span>
                <span>严重度: {finding.severity}</span>
                <span>置信度 {formatPercent(finding.confidence)}</span>
                <span className="evidence-link">证据: {formatIdList(finding.evidence_ids)}</span>
                <span>
                  缺口: {formatIdList(
                    finding.gaps.map((gap) => projectAgentUiText(gap, finding.execution_layer)),
                  )}
                </span>
              </div>
              <p className="muted">
                {projectAgentUiText(finding.rationale, finding.execution_layer)}
              </p>
            </div>
          ))}
        </article>
      ))}
    </div>
  );
}

function RcaWorkbenchPanel({ investigationId }: { investigationId: string }) {
  const workbenchQuery = useQuery<RcaWorkbench>({
    queryKey: ["rca-workbench", investigationId],
    queryFn: () => getRcaWorkbench(investigationId),
    enabled: Boolean(investigationId),
  });
  const legacyCandidates = workbenchQuery.data?.candidates ?? [];
  const run = workbenchQuery.data?.multi_agent_run ?? null;
  const persistedReview = workbenchQuery.data?.coordination_review;
  const review = isUsableV7Review(persistedReview, run) ? persistedReview : null;
  const candidates = selectVisibleCandidates(persistedReview, run, legacyCandidates);
  const visibleCandidateIds = new Set(candidates.map((candidate) => candidate.id));
  const hiddenCandidateIds = new Set(
    legacyCandidates
      .map((candidate) => candidate.id)
      .filter((candidateId) => !visibleCandidateIds.has(candidateId)),
  );
  const edges = (workbenchQuery.data?.graph_seed.edges ?? []).filter(
    (edge) => !hiddenCandidateIds.has(edge.source) && !hiddenCandidateIds.has(edge.target),
  );
  const baselineHypothesis = workbenchQuery.data?.investigation.hypotheses[0];
  const baselineCause = review?.baseline_cause_type ?? baselineHypothesis?.cause_type ?? "未知";
  const baselineConfidence = baselineHypothesis?.confidence;
  const decisionStatus = review?.decision_status ?? "fallback";
  const selectedCause = selectDisplayedCause(
    decisionStatus,
    review?.selected_cause_type,
    baselineCause,
  );
  const findings = workbenchQuery.data?.findings ?? [];
  const findingsByAgent = groupFindingsByAgent(findings);
  const v7Findings = findings.filter(
    (finding) => finding.execution_layer === "openai_agents_sdk",
  );
  const customFindings = findings.filter(
    (finding) => finding.execution_layer !== "openai_agents_sdk",
  );
  const v7FindingsByAgent = groupFindingsByAgent(v7Findings);
  const customFindingsByAgent = groupFindingsByAgent(customFindings);
  const revisionCount = v7Findings.filter((finding) => finding.analysis_round === 2).length;
  const reviewSummary = review
    ? projectAgentUiText(review.summary ?? "", review.execution_layer)
    : "";
  const reviewUncertainty = review
    ? projectAgentUiText(review.uncertainty ?? "", review.execution_layer)
    : "";
  const safeRunFailureReason = projectAgentUiText(
    run?.failure_reason ?? "",
    run ? "openai_agents_sdk" : undefined,
  );
  const evidenceIds = (workbenchQuery.data?.evidence ?? []).map((item) => item.id);
  const recommendationTitles = (workbenchQuery.data?.investigation.actions ?? []).map(
    (action) => action.title,
  );
  const isEmpty =
    Object.keys(findingsByAgent).length === 0 && candidates.length === 0 && edges.length === 0 && !run;

  return (
    <section className="panel rca-workbench">
      <div className="panel-heading">
        <h2>{run ? "混合 RCA 裁决" : "Agent 判断"}</h2>
        <span>{workbenchQuery.data?.findings.length ?? 0}</span>
      </div>
      {workbenchQuery.isLoading ? (
        <div className="empty-state">加载 RCA 工作台...</div>
      ) : workbenchQuery.isError ? (
        <p className="error">RCA 工作台加载失败：{workbenchQuery.error.message}</p>
      ) : isEmpty ? (
        <div className="empty-state">暂无 RCA 工作台数据。</div>
      ) : (
        <>
          {run ? (
            <>
              <div className="detail-grid">
                <div>
                  <span className="label">运行状态</span>
                  <StatusBadge value={run.status} />
                </div>
                <div>
                  <span className="label">裁决</span>
                  <strong>{decisionLabels[decisionStatus]}</strong>
                </div>
                <div>
                  <span className="label">确定性 RCA baseline</span>
                  <strong>{baselineCause}</strong>
                  {baselineConfidence === undefined ? null : (
                    <span>{formatPercent(baselineConfidence)}</span>
                  )}
                </div>
                <div>
                  <span className="label">最终候选</span>
                  <strong>{selectedCause}</strong>
                </div>
              </div>

              <div className="compact-list">
                <article className="compact-row">
                  <strong>事实（证据）</strong>
                  <p className="evidence-link">证据 IDs: {formatIdList(evidenceIds)}</p>
                </article>
                <article className="compact-row">
                  <strong>确定性推断</strong>
                  <p>{baselineHypothesis?.summary ?? baselineCause}</p>
                </article>
                <article className="compact-row">
                  <strong>Agent 推断</strong>
                  <p>{reviewSummary || "本次多 Agent 复核未形成可持久化裁决。"}</p>
                  <p>
                    冲突复核: {revisionCount > 0 ? `${revisionCount} 条第 2 轮修订` : "未触发第 2 轮修订"}
                  </p>
                </article>
                <article className="compact-row">
                  <strong>建议（仅建议，未执行）</strong>
                  <p>{recommendationTitles.length > 0 ? recommendationTitles.join("；") : "暂无建议"}</p>
                </article>
                <article className="compact-row">
                  <strong>不确定性</strong>
                  <p>{reviewUncertainty || safeRunFailureReason || "本次未提供额外不确定性说明。"}</p>
                  {safeRunFailureReason ? (
                    <p className="error">失败原因: {safeRunFailureReason}</p>
                  ) : null}
                </article>
              </div>

              <div className="panel-heading">
                <h2>Agent 推断</h2>
                <span>{v7Findings.length}</span>
              </div>
            </>
          ) : null}
          <AgentFindingList findingsByAgent={run ? v7FindingsByAgent : findingsByAgent} />
          {run ? (
            <>
              <div className="panel-heading">
                <h2>既有 Agent 判断</h2>
                <span>{customFindings.length}</span>
              </div>
              <AgentFindingList findingsByAgent={customFindingsByAgent} />
            </>
          ) : null}

          <div className="panel-heading">
            <h2>候选根因排序</h2>
            <span>{candidates.length}</span>
          </div>
          <div className="compact-list">
            {candidates.map((candidate) => (
              <article className="compact-row" key={candidate.id}>
                <div className="score-row">
                  <strong>#{candidate.rank} {candidate.cause_type}</strong>
                  <span>{formatPercent(candidate.confidence)}</span>
                </div>
                <p>{projectAgentUiText(candidate.summary, persistedReview?.execution_layer)}</p>
                <p>{projectAgentUiText(candidate.rationale, persistedReview?.execution_layer)}</p>
                <div className="process-detail">
                  <span>支持判断: {formatIdList(candidate.supporting_finding_ids)}</span>
                  <span>反对判断: {formatIdList(candidate.contradicting_finding_ids)}</span>
                  <span>支持证据: {formatIdList(candidate.supporting_evidence_ids)}</span>
                  <span>反对证据: {formatIdList(candidate.contradicting_evidence_ids)}</span>
                </div>
                <p className="muted">
                  {projectAgentUiText(candidate.uncertainty, persistedReview?.execution_layer)}
                </p>
              </article>
            ))}
          </div>

          <div className="panel-heading">
            <h2>证据链预览</h2>
            <span>{edges.length}</span>
          </div>
          <div className="compact-list">
            {edges.map((edge, index) => (
              <div className="compact-row evidence-link" key={`${edge.source}-${edge.relation}-${edge.target}-${index}`}>
                {edge.source} {edge.relation} {edge.target}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function ReActTracePanel({ investigationId }: { investigationId: string }) {
  const traceQuery = useQuery<ReActTrace | null>({
    queryKey: ["react-trace", investigationId],
    queryFn: () => getReActTrace(investigationId),
    enabled: Boolean(investigationId),
  });
  const trace = traceQuery.data;

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>ReAct 推理过程</h2>
        <span>只读</span>
      </div>
      {traceQuery.isLoading ? (
        <div className="empty-state">加载 ReAct 轨迹...</div>
      ) : traceQuery.isError ? (
        <p className="error">ReAct 轨迹加载失败：{traceQuery.error.message}</p>
      ) : !trace ? (
        <div className="empty-state">暂无 ReAct 轨迹。只读代理未产生额外记录。</div>
      ) : (
        <div className="compact-list">
          {trace.final_answer ? <p>{trace.final_answer}</p> : null}
          {trace.steps.map((step) => (
            <article className="compact-row" key={step.step_number}>
              <strong>
                #{step.step_number} {step.tool_name ?? step.status}
              </strong>
              {step.assistant_text ? <p>{step.assistant_text}</p> : null}
              {step.observation ? <p>{step.observation}</p> : null}
              <div className="process-detail">
                <span>状态: {step.status}</span>
                <span>输入: {JSON.stringify(step.tool_input)}</span>
                <span>证据: {formatIdList(step.output_evidence_ids)}</span>
              </div>
              {step.error_message ? <p className="error">{step.error_message}</p> : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function RecommendedActions({ investigation }: { investigation: InvestigationRecord }) {
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: ({
      actionId,
      status,
      note,
    }: {
      actionId: string;
      status: ActionStatus;
      note: string;
    }) => updateActionStatus(investigation.id, actionId, status, note),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["investigation", investigation.id] });
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
    },
  });

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>建议动作</h2>
        <span>{investigation.actions.length}</span>
      </div>
      <div className="action-list">
        {investigation.actions.map((action) => (
          <article className="workflow-item" key={action.id}>
            <div className="workflow-heading">
              <strong>{action.title}</strong>
              <StatusBadge value={action.status} />
            </div>
            <p>{action.description}</p>
            <div className="meta-row">
              <span>风险级别: {action.risk_level}</span>
              <span>{action.requires_approval ? "需记录审批状态" : "可选记录审批状态"}</span>
            </div>
            <div className="control-row">
              <select
                aria-label={`${action.title} 的动作状态`}
                defaultValue={action.status}
                onChange={(event) =>
                  mutation.mutate({
                    actionId: action.id,
                    status: event.target.value as ActionStatus,
                    note: notes[action.id] ?? action.note ?? "",
                  })
                }
              >
                {actionStatuses.map((status) => (
                  <option key={status} value={status}>
                    {statusLabels[status] ?? status}
                  </option>
                ))}
              </select>
              <input
                placeholder="记录审批状态"
                value={notes[action.id] ?? action.note ?? ""}
                onChange={(event) => setNotes({ ...notes, [action.id]: event.target.value })}
              />
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function VerificationSuggestions({ investigation }: { investigation: InvestigationRecord }) {
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const mutation = useMutation({
    mutationFn: ({
      verificationId,
      status,
      resultNote,
    }: {
      verificationId: string;
      status: VerificationStatus;
      resultNote: string;
    }) => updateVerificationStatus(investigation.id, verificationId, status, resultNote),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["investigation", investigation.id] });
      queryClient.invalidateQueries({ queryKey: ["investigations"] });
    },
  });

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>验证建议</h2>
        <span>{investigation.verification_suggestions.length}</span>
      </div>
      <div className="action-list">
        {investigation.verification_suggestions.map((verification) => (
          <article className="workflow-item" key={verification.id}>
            <div className="workflow-heading">
              <strong>{verification.title}</strong>
              <StatusBadge value={verification.status} />
            </div>
            <p>{verification.description}</p>
            <p className="muted">预期信号: {verification.expected_signal}</p>
            <div className="control-row">
              <select
                aria-label={`${verification.title} 的验证结果`}
                defaultValue={verification.status}
                onChange={(event) =>
                  mutation.mutate({
                    verificationId: verification.id,
                    status: event.target.value as VerificationStatus,
                    resultNote: notes[verification.id] ?? verification.result_note ?? "",
                  })
                }
              >
                {verificationStatuses.map((status) => (
                  <option key={status} value={status}>
                    {statusLabels[status] ?? status}
                  </option>
                ))}
              </select>
              <input
                placeholder="记录验证结果"
                value={notes[verification.id] ?? verification.result_note ?? ""}
                onChange={(event) =>
                  setNotes({ ...notes, [verification.id]: event.target.value })
                }
              />
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

type AgentProcessData = {
  plan: DiagnosisPlan | null;
  tasks: DiagnosisTask[];
  executions: AgentExecution[];
  contextFacts: ContextFact[];
  toolCalls: ToolCallRecord[];
  memoryHits: MemoryItem[];
  taskGraph: TaskGraph;
};

function AgentProcessPanels({ investigationId }: { investigationId: string }) {
  const processQuery = useQuery<AgentProcessData>({
    queryKey: ["agent-process", investigationId],
    queryFn: async () => {
      const [plan, tasks, executions, contextFacts, toolCalls, memoryHits, taskGraph] =
        await Promise.all([
          getPlan(investigationId),
          getTasks(investigationId),
          getAgentExecutions(investigationId),
          getContextFacts(investigationId),
          getToolCalls(investigationId),
          getMemoryHits(investigationId),
          getTaskGraph(investigationId),
        ]);
      return { plan, tasks, executions, contextFacts, toolCalls, memoryHits, taskGraph };
    },
  });
  const data = processQuery.data;
  const tasks = data?.tasks.length ? data.tasks : data?.plan?.tasks ?? [];
  const executions = data?.executions ?? [];
  const toolCalls = data?.toolCalls ?? [];
  const contextFacts = data?.contextFacts ?? [];
  const memoryHits = data?.memoryHits ?? [];
  const dependencyCount = data?.taskGraph.edges.length ?? 0;
  const errorMessage = processQuery.isError ? processQuery.error.message : "";

  return (
    <>
      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>任务规划</h2>
          <span>
            {tasks.length} 项, 依赖 {dependencyCount} 条
          </span>
        </div>
        {processQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : errorMessage ? (
          <p className="error">{errorMessage}</p>
        ) : tasks.length === 0 ? (
          <div className="empty-state">暂无任务规划。</div>
        ) : (
          <div className="process-list">
            {tasks.map((task) => (
              <article className="process-item" key={task.id}>
                <div className="workflow-heading">
                  <strong>{task.title}</strong>
                  <StatusBadge value={task.status} />
                </div>
                <div className="process-detail">
                  <span>类型: {task.task_type}</span>
                  <span>Agent: {task.agent_name}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>Agent 执行过程</h2>
          <span>{executions.length}</span>
        </div>
        {processQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : errorMessage ? (
          <p className="error">{errorMessage}</p>
        ) : executions.length === 0 ? (
          <div className="empty-state">暂无 Agent 执行记录。</div>
        ) : (
          <div className="process-list">
            {executions.map((execution) => (
              <article className="process-item" key={execution.id}>
                <div className="workflow-heading">
                  <strong>{execution.agent_name}</strong>
                  <StatusBadge value={execution.status} />
                </div>
                <p>{execution.summary ?? "暂无摘要"}</p>
                {execution.error_message ? <p className="error">{execution.error_message}</p> : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>工具调用</h2>
          <span>{toolCalls.length}</span>
        </div>
        {processQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : errorMessage ? (
          <p className="error">{errorMessage}</p>
        ) : toolCalls.length === 0 ? (
          <div className="empty-state">暂无工具调用。</div>
        ) : (
          <div className="process-list">
            {toolCalls.map((call) => (
              <article className="process-item" key={call.id}>
                <div className="workflow-heading">
                  <strong>{call.tool_name}</strong>
                  <StatusBadge value={call.status} />
                </div>
                <div className="process-detail">
                  <span>Agent: {call.agent_name}</span>
                  <span>证据: {formatIdList(call.output_evidence_ids)}</span>
                </div>
                {call.error_message ? <p className="error">{call.error_message}</p> : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>共享上下文</h2>
          <span>{contextFacts.length}</span>
        </div>
        {processQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : errorMessage ? (
          <p className="error">{errorMessage}</p>
        ) : contextFacts.length === 0 ? (
          <div className="empty-state">暂无共享上下文。</div>
        ) : (
          <div className="process-list">
            {contextFacts.map((fact) => (
              <article className="process-item" key={fact.id}>
                <div className="workflow-heading">
                  <strong>{fact.summary}</strong>
                  <span>{formatPercent(fact.confidence)}</span>
                </div>
                <div className="process-detail">
                  <span>类型: {fact.fact_type}</span>
                  <span>来源: {fact.source_agent}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel agent-process-panel">
        <div className="panel-heading">
          <h2>历史记忆</h2>
          <span>{memoryHits.length}</span>
        </div>
        {processQuery.isLoading ? (
          <div className="empty-state">加载中...</div>
        ) : errorMessage ? (
          <p className="error">{errorMessage}</p>
        ) : memoryHits.length === 0 ? (
          <div className="empty-state">暂无历史记忆。</div>
        ) : (
          <div className="process-list">
            {memoryHits.map((memory) => (
              <article className="process-item" key={memory.id}>
                <strong>{memory.summary}</strong>
                <div className="process-detail">
                  <span>类型: {memory.memory_type}</span>
                  <span>标签: {formatIdList(memory.tags)}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function ReportPanel({ investigation }: { investigation: InvestigationRecord }) {
  return (
    <section className="panel report-panel">
      <div className="panel-heading">
        <h2>诊断报告</h2>
      </div>
      {investigation.report ? (
        <MarkdownReport markdown={investigation.report.markdown} />
      ) : (
        <div className="empty-state">该诊断尚未生成报告。</div>
      )}
    </section>
  );
}

export default function App() {
  const [selectedId, setSelectedId] = useState<string>();
  const investigationsQuery = useQuery({
    queryKey: ["investigations"],
    queryFn: listInvestigations,
  });

  const investigations = useMemo(
    () =>
      [...(investigationsQuery.data ?? [])].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      ),
    [investigationsQuery.data],
  );

  const activeId = selectedId ?? investigations[0]?.id;
  const detailQuery = useQuery({
    queryKey: ["investigation", activeId],
    queryFn: () => getInvestigation(activeId as string),
    enabled: Boolean(activeId),
  });

  const activeInvestigation = detailQuery.data ?? investigations.find((item) => item.id === activeId);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>DiagOps 诊断控制台</h1>
          <p>API 地址: {API_BASE_URL}</p>
        </div>
        <div className="topbar-stats">
          <span>共 {investigations.length} 条诊断</span>
          <span>记录审批状态和验证结果</span>
        </div>
      </header>

      <div className="workspace">
        <aside className="left-column">
          <ManualInvestigationForm onCreated={setSelectedId} />
          {investigationsQuery.isError ? (
            <div className="panel error-panel">{investigationsQuery.error.message}</div>
          ) : null}
          <InvestigationList
            investigations={investigations}
            selectedId={activeId}
            onSelect={setSelectedId}
          />
        </aside>

        <section className="center-column">
          {activeInvestigation ? (
            <>
              <InvestigationDetail investigation={activeInvestigation} />
              <EvidenceList investigation={activeInvestigation} />
              <Hypotheses investigation={activeInvestigation} />
              <RcaWorkbenchPanel investigationId={activeInvestigation.id} />
              <ReActTracePanel investigationId={activeInvestigation.id} />
            </>
          ) : (
            <div className="panel empty-state">选择或创建一个诊断开始。</div>
          )}
        </section>

        <aside className="right-column">
          {activeInvestigation ? (
            <>
              <RecommendedActions investigation={activeInvestigation} />
              <VerificationSuggestions investigation={activeInvestigation} />
              <AgentProcessPanels investigationId={activeInvestigation.id} />
              <ReportPanel investigation={activeInvestigation} />
            </>
          ) : (
            <div className="panel empty-state">审批、验证和报告会显示在这里。</div>
          )}
        </aside>
      </div>
    </main>
  );
}
