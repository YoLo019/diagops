"""Runtime Workbench 前端 projection/selection 的可执行行为测试。

通过 node 直接执行 `frontend/src/runtimeProjection.ts`（纯函数模块），
验证 phase 时间线、agent 泳道、当前 Run 选择、操作开关和 allowlist 细节投影，
不再断言源码字符串。类型与接口由 frontend production build 和
backend Runtime API tests 覆盖。
"""

import json
import os
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).parents[2] / "frontend"

_DRIVER = """
import {
  allowlistedRuntimeDetail,
  canCancelRuntimeRun,
  canReplayRuntimeRun,
  canResumeRuntimeRun,
  groupAgentSwimlanes,
  projectPhaseTimeline,
  selectCurrentRuntimeRun,
} from './src/runtimeProjection.ts';

const event = (overrides) => ({
  sequence: 1,
  event_type: 'phase.started',
  phase: 'collect',
  actor_type: 'system',
  actor_name: null,
  tool_call_id: null,
  evidence_ids: [],
  safe_payload: {},
  occurred_at: '2026-07-30T12:00:00Z',
  schema_version: 1,
  ...overrides,
});
const run = (overrides) => ({
  id: 'run-x',
  run_kind: 'live',
  status: 'running',
  ...overrides,
});

const calls = JSON.parse(process.env.PROJECTION_CALLS);
const registry = {
  timeline: (events) => projectPhaseTimeline(events),
  lanes: (events) => groupAgentSwimlanes(events),
  select: (runs) => selectCurrentRuntimeRun(runs),
  detail: (item) => allowlistedRuntimeDetail(item),
  controls: (item) => [
    canCancelRuntimeRun(item),
    canResumeRuntimeRun(item),
    canReplayRuntimeRun(item),
  ],
};
const results = calls.map(({ fn, args }) => {
  const value = registry[fn](...args);
  return value === undefined ? null : value;
});
process.stdout.write(JSON.stringify({ results, event, run }));
"""


def _execute(calls: list[dict[str, object]]) -> list[object]:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", _DRIVER],
        cwd=FRONTEND,
        env={**os.environ, "PROJECTION_CALLS": json.dumps(calls)},
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)["results"]


def _event(overrides: dict[str, object]) -> dict[str, object]:
    base = {
        "sequence": 1,
        "event_type": "phase.started",
        "phase": "collect",
        "actor_type": "system",
        "actor_name": None,
        "tool_call_id": None,
        "evidence_ids": [],
        "safe_payload": {},
        "occurred_at": "2026-07-30T12:00:00Z",
        "schema_version": 1,
    }
    base.update(overrides)
    return base


def _run(overrides: dict[str, object]) -> dict[str, object]:
    base = {"id": "run-x", "run_kind": "live", "status": "running"}
    base.update(overrides)
    return base


def test_phase_timeline_projects_known_events_and_durations() -> None:
    events = [
        _event({"phase": "collect", "occurred_at": "2026-07-30T12:00:00Z"}),
        _event(
            {
                "sequence": 2,
                "event_type": "phase.completed",
                "phase": "collect",
                "occurred_at": "2026-07-30T12:00:04Z",
                "safe_payload": {"status": "completed"},
            }
        ),
        # schema 不匹配与未知类型不得进入时间线。
        _event({"sequence": 3, "schema_version": 2, "phase": "analyze"}),
        _event(
            {
                "sequence": 4,
                "event_type": "phase.custom",
                "phase": "analyze",
                "occurred_at": "2026-07-30T12:00:05Z",
            }
        ),
    ]

    [timeline] = _execute([{"fn": "timeline", "args": [events]}])

    assert timeline == [
        {
            "phase": "collect",
            "status": "completed",
            "startedAt": "2026-07-30T12:00:00Z",
            "completedAt": "2026-07-30T12:00:04Z",
            "durationMs": 4000,
        }
    ]


def test_agent_swimlanes_group_sort_and_ignore_unknown_types() -> None:
    events = [
        _event(
            {
                "sequence": 2,
                "event_type": "agent.completed",
                "actor_name": "LogAgent",
            }
        ),
        _event(
            {
                "sequence": 1,
                "event_type": "agent.started",
                "actor_name": "LogAgent",
            }
        ),
        _event(
            {
                "sequence": 3,
                "event_type": "agent.started",
                "actor_name": None,
            }
        ),
        _event({"sequence": 4, "event_type": "agent.unknown", "actor_name": "X"}),
    ]

    [lanes] = _execute([{"fn": "lanes", "args": [events]}])

    assert [lane["agentName"] for lane in lanes] == ["LogAgent", "unknown"]
    assert [item["sequence"] for item in lanes[0]["events"]] == [1, 2]


def test_current_run_selection_prefers_actionable_live_runs() -> None:
    replay_newer = _run({"id": "replay", "run_kind": "replay", "status": "completed"})
    live_done = _run({"id": "done", "status": "completed"})
    live_running = _run({"id": "running", "status": "running"})
    live_interrupted = _run({"id": "interrupted", "status": "interrupted"})

    results = _execute(
        [
            {"fn": "select", "args": [[replay_newer, live_done, live_running]]},
            {"fn": "select", "args": [[replay_newer, live_done, live_interrupted]]},
            {"fn": "select", "args": [[replay_newer, live_done]]},
            {"fn": "select", "args": [[replay_newer]]},
        ]
    )

    assert [item["id"] for item in results] == [
        "running",
        "interrupted",
        "done",
        "replay",
    ]


def test_run_control_guards_follow_run_status() -> None:
    statuses = [
        "created",
        "running",
        "cancelling",
        "interrupted",
        "completed",
        "failed",
        "cancelled",
    ]

    results = _execute(
        [{"fn": "controls", "args": [_run({"status": status})]} for status in statuses]
    )

    cancel, resume, replay = zip(*results, strict=False)
    assert list(cancel) == [True, True, True, False, False, False, False]
    assert list(resume) == [False, False, False, True, False, False, False]
    assert list(replay) == [False, False, False, False, True, True, True]


def test_allowlisted_detail_drops_non_allowlisted_fields() -> None:
    item = _event(
        {
            "safe_payload": {"note": "ok"},
            "raw_prompt": "secret prompt",
            "provider_payload": {"token": "secret"},
            "chain_of_thought": "hidden reasoning",
        }
    )

    [detail] = _execute([{"fn": "detail", "args": [item]}])

    assert set(detail) == {
        "sequence",
        "event_type",
        "phase",
        "actor_type",
        "actor_name",
        "tool_call_id",
        "evidence_ids",
        "safe_payload",
        "occurred_at",
        "schema_version",
    }
    assert "secret" not in json.dumps(detail)
