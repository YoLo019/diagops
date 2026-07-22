import type { JsonValue, RuntimeEvent, RuntimeRun } from "./api";

export type PhaseTimelineItem = {
  phase: string;
  status: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
};

export type AgentSwimlane = {
  agentName: string;
  events: RuntimeEvent[];
};

type KnownPhaseEventType =
  | "phase.started"
  | "phase.completed"
  | "phase.failed"
  | "phase.skipped";

type KnownAgentEventType = "agent.started" | "agent.completed" | "agent.failed";

const KNOWN_PHASE_EVENT_TYPES = new Set<KnownPhaseEventType>([
  "phase.started",
  "phase.completed",
  "phase.failed",
  "phase.skipped",
]);

const KNOWN_AGENT_EVENT_TYPES = new Set<KnownAgentEventType>([
  "agent.started",
  "agent.completed",
  "agent.failed",
]);

function isKnownPhaseEventType(value: string): value is KnownPhaseEventType {
  return KNOWN_PHASE_EVENT_TYPES.has(value as KnownPhaseEventType);
}

function isKnownAgentEventType(value: string): value is KnownAgentEventType {
  return KNOWN_AGENT_EVENT_TYPES.has(value as KnownAgentEventType);
}

export function projectPhaseTimeline(events: RuntimeEvent[]): PhaseTimelineItem[] {
  const phases = new Map<string, PhaseTimelineItem>();
  for (const event of events) {
    if (
      event.schema_version !== 1 ||
      !event.phase ||
      !isKnownPhaseEventType(event.event_type)
    ) continue;
    const current = phases.get(event.phase) ?? { phase: event.phase, status: "pending" };
    if (event.event_type === "phase.started") {
      current.startedAt = event.occurred_at;
      current.status = "running";
    } else {
      current.completedAt = event.occurred_at;
      current.status = String(event.safe_payload.status ?? event.event_type.split(".")[1]);
      if (current.startedAt) {
        current.durationMs = Math.max(
          0,
          new Date(current.completedAt).getTime() - new Date(current.startedAt).getTime(),
        );
      }
    }
    phases.set(event.phase, current);
  }
  return [...phases.values()];
}

export function groupAgentSwimlanes(events: RuntimeEvent[]): AgentSwimlane[] {
  const lanes = new Map<string, RuntimeEvent[]>();
  for (const event of events) {
    if (event.schema_version !== 1 || !isKnownAgentEventType(event.event_type)) continue;
    const name = event.actor_name ?? "unknown";
    lanes.set(name, [...(lanes.get(name) ?? []), event]);
  }
  return [...lanes.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([agentName, laneEvents]) => ({
      agentName,
      events: laneEvents.sort((left, right) => left.sequence - right.sequence),
    }));
}

export function allowlistedRuntimeDetail(event?: RuntimeEvent): Record<string, JsonValue> {
  if (!event) return {};
  return {
    sequence: event.sequence,
    event_type: event.event_type,
    phase: event.phase ?? null,
    actor_type: event.actor_type,
    actor_name: event.actor_name ?? null,
    tool_call_id: event.tool_call_id ?? null,
    evidence_ids: event.evidence_ids,
    safe_payload: event.safe_payload,
    occurred_at: event.occurred_at,
    schema_version: event.schema_version,
  };
}

export function canCancelRuntimeRun(run: RuntimeRun) {
  return ["created", "running", "cancelling"].includes(run.status);
}

export function canResumeRuntimeRun(run: RuntimeRun) {
  return run.status === "interrupted";
}

export function canReplayRuntimeRun(run: RuntimeRun) {
  return ["completed", "failed", "cancelled"].includes(run.status);
}

export function selectCurrentRuntimeRun(runs: RuntimeRun[]) {
  const actionable = ["created", "running", "cancelling", "interrupted"];
  return runs.find(
    (run) => run.run_kind === "live" && actionable.includes(run.status),
  ) ?? runs.find((run) => run.run_kind === "live") ?? runs[0];
}
