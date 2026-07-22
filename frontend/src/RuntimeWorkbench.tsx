import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelRuntimeRun,
  createRuntimeRun,
  diffRuntimeRuns,
  getRuntimeRun,
  listRuntimeRuns,
  replayRuntimeRun,
  resumeRuntimeRun,
  type InvestigationSummary,
  type RuntimeEvent,
  type RuntimeRun,
} from "./api";
import {
  allowlistedRuntimeDetail,
  canCancelRuntimeRun,
  canReplayRuntimeRun,
  canResumeRuntimeRun,
  groupAgentSwimlanes,
  projectPhaseTimeline,
  selectCurrentRuntimeRun,
} from "./runtimeProjection";
import { useRuntimeEvents } from "./useRuntimeEvents";

type Props = {
  investigations: InvestigationSummary[];
  selectedInvestigationId?: string;
  onSelectInvestigation: (id: string) => void;
};

function runLabel(run: RuntimeRun) {
  return `${run.run_kind} · ${run.status} · ${new Date(run.created_at).toLocaleString()}`;
}

export function RuntimeWorkbench({
  investigations,
  selectedInvestigationId,
  onSelectInvestigation,
}: Props) {
  const queryClient = useQueryClient();
  const investigationId = selectedInvestigationId ?? investigations[0]?.id;
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [selectedEvent, setSelectedEvent] = useState<RuntimeEvent>();
  const sessionRunQueries = useQueries({
    queries: investigations.map((investigation) => ({
      queryKey: ["runtime-runs", investigation.id],
      queryFn: () => listRuntimeRuns(investigation.id),
      refetchInterval: 5_000,
    })),
  });
  const sessionRuns = new Map(
    investigations.map((investigation, index) => [
      investigation.id,
      sessionRunQueries[index]?.data ?? [],
    ]),
  );
  const runs = investigationId ? sessionRuns.get(investigationId) ?? [] : [];
  const currentRun = selectCurrentRuntimeRun(runs);

  useEffect(() => {
    if (!runs.length) setSelectedRunId(undefined);
    else if (!selectedRunId || !runs.some((run) => run.id === selectedRunId)) {
      setSelectedRunId(currentRun?.id);
    }
  }, [currentRun?.id, runs, selectedRunId]);

  const selectedRun = runs.find((run) => run.id === selectedRunId) ?? currentRun;
  const historical = Boolean(selectedRun && currentRun && selectedRun.id !== currentRun.id);
  const detailQuery = useQuery({
    queryKey: ["runtime-run", selectedRun?.id],
    queryFn: () => getRuntimeRun(selectedRun!.id),
    enabled: Boolean(selectedRun),
  });
  const eventState = useRuntimeEvents(selectedRun?.id);
  const phases = useMemo(
    () => projectPhaseTimeline(eventState.events),
    [eventState.events],
  );
  const agentLanes = useMemo(
    () => groupAgentSwimlanes(eventState.events),
    [eventState.events],
  );

  const invalidateRuntime = async () => {
    await queryClient.invalidateQueries({ queryKey: ["runtime-runs", investigationId] });
    await queryClient.invalidateQueries({ queryKey: ["runtime-run", selectedRun?.id] });
    await queryClient.invalidateQueries({ queryKey: ["runtime-events", selectedRun?.id] });
  };
  const createMutation = useMutation({
    mutationFn: () =>
      createRuntimeRun(investigationId as string, {
        strategy: "fixed",
        run_reason: "manual_rerun",
        parent_run_id: currentRun?.id,
      }),
    onSuccess: invalidateRuntime,
  });
  const cancelMutation = useMutation({
    mutationFn: () => cancelRuntimeRun(selectedRun!.id),
    onSuccess: invalidateRuntime,
  });
  const resumeMutation = useMutation({
    mutationFn: () => resumeRuntimeRun(selectedRun!.id),
    onSuccess: invalidateRuntime,
  });
  const replayMutation = useMutation({
    mutationFn: () => replayRuntimeRun(selectedRun!.id),
    onSuccess: invalidateRuntime,
  });
  const compareTarget = runs.find((run) => run.id !== selectedRun?.id);
  const diffMutation = useMutation({
    mutationFn: () => diffRuntimeRuns(selectedRun!.id, compareTarget!.id),
  });
  const detail = allowlistedRuntimeDetail(
    selectedEvent?.run_id === selectedRun?.id ? selectedEvent : undefined,
  );
  const checkpoints = detailQuery.data?.checkpoints ?? [];

  return (
    <div className="runtime-workbench">
      <aside className="runtime-panel runtime-sessions">
        <div className="panel-heading">
          <h2>多 Investigation 会话</h2>
          <span>{investigations.length}</span>
        </div>
        <p className="runtime-scope-note">会话并行与单 Run 内 Agent 并行相互独立</p>
        <div className="runtime-session-list">
          {investigations.map((investigation) => (
            <button
              className={investigation.id === investigationId ? "active" : ""}
              key={investigation.id}
              onClick={() => onSelectInvestigation(investigation.id)}
              type="button"
            >
              <strong>{investigation.service}</strong>
              <span>{selectCurrentRuntimeRun(sessionRuns.get(investigation.id) ?? [])?.status ?? investigation.status}</span>
              <small>{investigation.runtime_available ? "Runtime available" : "Legacy record"}</small>
            </button>
          ))}
        </div>
        <div className="runtime-run-header">
          <h3>当前 Run</h3>
          <button
            disabled={
              !investigationId ||
              createMutation.isPending ||
              Boolean(currentRun && canCancelRuntimeRun(currentRun))
            }
            onClick={() => createMutation.mutate()}
            type="button"
          >
            新建 Run
          </button>
        </div>
        <div className="runtime-run-history">
          {runs.map((run) => (
            <button
              className={run.id === selectedRun?.id ? "active" : ""}
              key={run.id}
              onClick={() => setSelectedRunId(run.id)}
              type="button"
            >
              <span>{runLabel(run)}</span>
              {run.id !== currentRun?.id ? <small>历史 Run（只读）</small> : null}
            </button>
          ))}
        </div>
      </aside>

      <section className="runtime-panel runtime-execution">
        <div className="runtime-run-controls">
          <button
            disabled={!selectedRun || historical || !canCancelRuntimeRun(selectedRun)}
            onClick={() => cancelMutation.mutate()}
            type="button"
          >取消</button>
          <button
            disabled={!selectedRun || historical || !canResumeRuntimeRun(selectedRun)}
            onClick={() => resumeMutation.mutate()}
            type="button"
          >恢复</button>
          <button
            disabled={!selectedRun || !canReplayRuntimeRun(selectedRun)}
            onClick={() => replayMutation.mutate()}
            type="button"
          >Replay</button>
          <button
            disabled={!selectedRun || !compareTarget}
            onClick={() => diffMutation.mutate()}
            type="button"
          >Diff</button>
          <span>{eventState.connected ? "实时连接" : "durable catch-up"}</span>
        </div>
        <h2>Phase Timeline</h2>
        <div className="runtime-phase-timeline">
          {phases.map((phase) => (
            <article key={phase.phase} data-status={phase.status}>
              <strong>{phase.phase}</strong>
              <span>{phase.status}</span>
              <small>{phase.durationMs === undefined ? "进行中" : `${phase.durationMs} ms`}</small>
            </article>
          ))}
        </div>
        <h2>Agent Swimlanes</h2>
        <div className="runtime-agent-lanes">
          {agentLanes.map((lane) => (
            <section key={lane.agentName}>
              <strong>{lane.agentName}</strong>
              <div>
                {lane.events.map((event) => (
                  <button key={event.id} onClick={() => setSelectedEvent(event)} type="button">
                    #{event.sequence} {event.event_type}
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      </section>

      <aside className="runtime-panel runtime-detail">
        <h2>Event / Tool / Evidence / Checkpoint</h2>
        <div className="runtime-event-list">
          {eventState.events.map((event) => (
            <button key={event.id} onClick={() => setSelectedEvent(event)} type="button">
              <span>#{event.sequence}</span>
              <strong>{event.event_type}</strong>
            </button>
          ))}
        </div>
        <pre>{JSON.stringify(detail, null, 2)}</pre>
        <h3>Checkpoint refs</h3>
        <ul>
          {checkpoints.map((checkpoint) => (
            <li key={checkpoint.id}>
              {checkpoint.completed_phase} · #{checkpoint.event_sequence} · {checkpoint.id}
            </li>
          ))}
        </ul>
        {diffMutation.data ? (
          <pre>{JSON.stringify(diffMutation.data.sections, null, 2)}</pre>
        ) : null}
      </aside>
    </div>
  );
}
