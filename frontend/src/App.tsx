import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ActionStatus,
  API_BASE_URL,
  InvestigationRecord,
  VerificationStatus,
  createManualInvestigation,
  getInvestigation,
  listInvestigations,
  updateActionStatus,
  updateVerificationStatus,
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
