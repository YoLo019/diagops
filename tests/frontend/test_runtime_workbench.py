from pathlib import Path

FRONTEND = Path(__file__).parents[2] / "frontend"


def test_runtime_api_types_and_all_endpoints_are_declared() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")

    for type_name in [
        "JsonValue",
        "RuntimeRun",
        "RuntimeAttempt",
        "RuntimeEvent",
        "RuntimeCheckpoint",
        "ReplayReport",
        "RuntimeRunDiff",
    ]:
        assert f"export type {type_name}" in api
    assert "safe_payload: Record<string, JsonValue>" in api
    for method in [
        "createRuntimeRun",
        "listRuntimeRuns",
        "getRuntimeRun",
        "getRuntimeEvents",
        "cancelRuntimeRun",
        "resumeRuntimeRun",
        "replayRuntimeRun",
        "diffRuntimeRuns",
        "runtimeEventStreamUrl",
    ]:
        assert f"function {method}" in api
    for prohibited in ["raw_prompt", "provider_payload", "chain_of_thought"]:
        assert prohibited not in api


def test_runtime_event_hook_is_keyed_deduplicated_and_durable_on_error() -> None:
    hook = (FRONTEND / "src" / "useRuntimeEvents.ts").read_text(encoding="utf-8")

    for source_contract in [
        "new Map<string, RuntimeEventState>()",
        "new EventSource(",
        "runtimeEventStreamUrl(runId, current.lastSequence)",
        "event.sequence <= current.lastSequence",
        "getRuntimeEvents(runId, current.lastSequence",
        "Math.min(",
        "source.close()",
        '"runtime.opaque"',
    ]:
        assert source_contract in hook


def test_runtime_event_hook_never_returns_the_previous_runs_snapshot() -> None:
    hook = (FRONTEND / "src" / "useRuntimeEvents.ts").read_text(encoding="utf-8")
    workbench = (FRONTEND / "src" / "RuntimeWorkbench.tsx").read_text(
        encoding="utf-8"
    )

    assert "type SelectedRuntimeEventState" in hook
    assert "selected.runId === runId" in hook
    assert "{ runId, value:" in hook
    assert "selectedEvent?.run_id === selectedRun?.id" in workbench


def test_runtime_session_list_polls_each_current_run_status() -> None:
    workbench = (FRONTEND / "src" / "RuntimeWorkbench.tsx").read_text(
        encoding="utf-8"
    )

    assert "useQueries" in workbench
    assert "selectCurrentRuntimeRun(sessionRuns.get(investigation.id) ?? [])?.status" in workbench
    assert "refetchInterval: 5_000" in workbench


def test_current_runtime_run_prefers_actionable_live_over_newer_replay() -> None:
    workbench = (FRONTEND / "src" / "RuntimeWorkbench.tsx").read_text(
        encoding="utf-8"
    )
    projection = (FRONTEND / "src" / "runtimeProjection.ts").read_text(
        encoding="utf-8"
    )

    assert "selectCurrentRuntimeRun" in workbench
    assert "const currentRun = selectCurrentRuntimeRun(runs)" in workbench
    assert 'run.run_kind === "live"' in projection
    assert '"created", "running", "cancelling", "interrupted"' in projection


def test_runtime_workbench_has_three_panels_history_and_valid_controls() -> None:
    workbench = (FRONTEND / "src" / "RuntimeWorkbench.tsx").read_text(
        encoding="utf-8"
    )
    projection = (FRONTEND / "src" / "runtimeProjection.ts").read_text(
        encoding="utf-8"
    )

    for label in [
        "多 Investigation 会话",
        "当前 Run",
        "Phase Timeline",
        "Agent Swimlanes",
        "Event / Tool / Evidence / Checkpoint",
        "历史 Run（只读）",
        "会话并行与单 Run 内 Agent 并行相互独立",
    ]:
        assert label in workbench
    for control in [
        "cancelRuntimeRun",
        "resumeRuntimeRun",
        "replayRuntimeRun",
        "diffRuntimeRuns",
        "invalidateQueries",
        "canCancelRuntimeRun",
        "canResumeRuntimeRun",
        "canReplayRuntimeRun",
    ]:
        assert control in workbench or control in projection
    assert "groupAgentSwimlanes" in projection
    assert "projectPhaseTimeline" in projection


def test_runtime_projection_only_promotes_known_schema_v1_events() -> None:
    projection = (FRONTEND / "src" / "runtimeProjection.ts").read_text(
        encoding="utf-8"
    )

    assert '.startsWith("phase.")' not in projection
    assert '.startsWith("agent.")' not in projection
    assert "event.schema_version !== 1" in projection
    for event_type in [
        "phase.started",
        "phase.completed",
        "phase.failed",
        "phase.skipped",
        "agent.started",
        "agent.completed",
        "agent.failed",
    ]:
        assert f'"{event_type}"' in projection


def test_app_exposes_runtime_view_and_responsive_styles() -> None:
    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    styles = (FRONTEND / "src" / "styles.css").read_text(encoding="utf-8")
    vite = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")

    assert "RuntimeWorkbench" in app
    assert "Runtime Workbench" in app
    assert ".runtime-workbench" in styles
    assert ".runtime-agent-lanes" in styles
    assert "@media (max-width: 760px)" in styles
    assert '"/runtime-runs": "http://127.0.0.1:8000"' in vite
