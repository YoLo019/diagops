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

function formatDate(value?: string | null) {
  if (!value) {
    return "n/a";
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

function humanize(value: string) {
  return value.replace(/_/g, " ");
}

function StatusBadge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{humanize(value)}</span>;
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
        <h2>Manual Investigation</h2>
      </div>
      <label>
        Incident note
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="checkout-service error rate increased after a deploy"
          required
        />
      </label>
      <div className="field-row">
        <label>
          Service
          <input value={service} onChange={(event) => setService(event.target.value)} required />
        </label>
        <label>
          Environment
          <input
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            required
          />
        </label>
      </div>
      {mutation.isError ? <p className="error">{mutation.error.message}</p> : null}
      <button type="submit" disabled={mutation.isPending || !text.trim()}>
        {mutation.isPending ? "Creating..." : "Create investigation"}
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
        <h2>Investigation List</h2>
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
                {item.event.service} · {item.event.environment}
              </span>
              <span className="item-footer">
                <StatusBadge value={item.status} />
                <span>{topHypothesis ? formatPercent(topHypothesis.confidence) : "0%"}</span>
              </span>
            </button>
          );
        })}
        {investigations.length === 0 ? (
          <div className="empty-state">No investigations recorded yet.</div>
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
        <h2>Investigation Detail</h2>
        <StatusBadge value={investigation.status} />
      </div>
      <div className="detail-grid">
        <div>
          <span className="label">Service</span>
          <strong>{investigation.event.service}</strong>
        </div>
        <div>
          <span className="label">Environment</span>
          <strong>{investigation.event.environment}</strong>
        </div>
        <div>
          <span className="label">Severity</span>
          <strong>{humanize(investigation.event.severity)}</strong>
        </div>
        <div>
          <span className="label">Started</span>
          <strong>{formatDate(investigation.event.started_at)}</strong>
        </div>
      </div>
      <h3>{investigation.event.title}</h3>
      <p>{investigation.event.description}</p>
      {topHypothesis ? (
        <div className="callout">
          <span className="label">Top hypothesis</span>
          <strong>{humanize(topHypothesis.cause_type)}</strong>
          <span>{formatPercent(topHypothesis.confidence)} confidence</span>
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
        <h2>Evidence List</h2>
        <span>{investigation.evidence.length}</span>
      </div>
      <div className="evidence-list">
        {investigation.evidence.map((item) => (
          <article className="evidence-item" key={item.id}>
            <div>
              <StatusBadge value={item.status} />
              <span className="provider">{humanize(item.provider)}</span>
              <span className="timestamp">{formatDate(item.timestamp)}</span>
            </div>
            <strong>{item.summary}</strong>
            <p>
              {humanize(item.kind)} · confidence {formatPercent(item.confidence)}
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
        <h2>Hypotheses</h2>
        <span>{investigation.hypotheses.length}</span>
      </div>
      <div className="hypothesis-list">
        {investigation.hypotheses.map((hypothesis) => (
          <article className="hypothesis-item" key={hypothesis.id}>
            <div className="score-row">
              <strong>{humanize(hypothesis.cause_type)}</strong>
              <span>{formatPercent(hypothesis.confidence)}</span>
            </div>
            <p>{hypothesis.summary}</p>
            {hypothesis.next_actions.length > 0 ? (
              <p className="muted">Next: {hypothesis.next_actions.join(", ")}</p>
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
        <h2>Recommended Actions</h2>
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
              <span>Risk: {humanize(action.risk_level)}</span>
              <span>{action.requires_approval ? "Approval required" : "Review optional"}</span>
            </div>
            <div className="control-row">
              <select
                aria-label={`Action status for ${action.title}`}
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
                    {humanize(status)}
                  </option>
                ))}
              </select>
              <input
                placeholder="Record approval note"
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
        <h2>Verification Suggestions</h2>
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
            <p className="muted">Expected signal: {verification.expected_signal}</p>
            <div className="control-row">
              <select
                aria-label={`Verification result for ${verification.title}`}
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
                    {humanize(status)}
                  </option>
                ))}
              </select>
              <input
                placeholder="Record verification result"
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
        <h2>Markdown Report</h2>
      </div>
      {investigation.report ? (
        <MarkdownReport markdown={investigation.report.markdown} />
      ) : (
        <div className="empty-state">Report has not been generated for this investigation.</div>
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
          <h1>DiagOps Investigation Console</h1>
          <p>API base: {API_BASE_URL}</p>
        </div>
        <div className="topbar-stats">
          <span>{investigations.length} investigations</span>
          <span>Records approval and verification status</span>
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
            <div className="panel empty-state">Select or create an investigation to begin.</div>
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
            <div className="panel empty-state">Workflow details will appear here.</div>
          )}
        </aside>
      </div>
    </main>
  );
}
