from backend.db.models import InvestigationRecord
from backend.domain.multi_agent import AuthorityMode
from backend.domain.runtime import execution_contract_digest, validate_v11_execution_contract
from backend.domain.v11_contracts import validate_v11_final_status
from backend.runtime.store import RuntimeNotFound


class V11ProjectionIntegrityError(ValueError):
    """表示公开投影没有可验证的 V11 RuntimeRun 所有者。"""


def ensure_v11_projection_owner(repository, runtime_store, record: InvestigationRecord) -> None:
    """拒绝没有匹配持久化运行与契约摘要的 Agent 权威投影。"""
    review = repository.get_coordination_review(record.id)
    runtime_ids: set[str] = set()
    has_agent_projection = False
    for projection in (record.multi_agent_run, record.report, review):
        if projection is None:
            continue
        if getattr(projection, "authority_mode", None) != AuthorityMode.AGENT:
            continue
        has_agent_projection = True
        runtime_id = getattr(projection, "runtime_run_id", None)
        if runtime_id is None:
            raise V11ProjectionIntegrityError("Agent projection lacks runtime owner")
        runtime_ids.add(runtime_id)

    active_runtime_id = record.active_runtime_run_id
    active_run = None
    if active_runtime_id is not None and not has_agent_projection:
        try:
            active_run = runtime_store.get_run(active_runtime_id)
        except (KeyError, RuntimeNotFound):
            # V10 旧记录可能保留 active owner，即使 runtime store 暂不可用；只有
            # 已识别为 V11 的运行才构成公开 Agent 投影契约。
            active_run = None
        if active_run is not None and active_run.is_v11:
            has_agent_projection = True

    if has_agent_projection and active_runtime_id is not None:
        runtime_ids.add(active_runtime_id)
    if has_agent_projection:
        for label, projections in (
            ("evidence", record.evidence),
            ("action", record.actions),
            ("verification", record.verification_suggestions),
        ):
            for projection in projections:
                runtime_id = getattr(projection, "runtime_run_id", None)
                if runtime_id is None:
                    raise V11ProjectionIntegrityError(
                        f"Agent {label} projection lacks runtime owner"
                    )
                runtime_ids.add(runtime_id)

    if not has_agent_projection:
        return

    if len(runtime_ids) != 1:
        raise V11ProjectionIntegrityError("Agent projection runtime owner mismatch")
    runtime_id = next(iter(runtime_ids))
    try:
        run = (
            active_run
            if active_run is not None and active_run.id == runtime_id
            else runtime_store.get_run(runtime_id)
        )
    except (KeyError, RuntimeNotFound) as exc:
        raise V11ProjectionIntegrityError("Agent projection RuntimeRun is unavailable") from exc
    if (
        not run.is_v11
        or run.authority_mode != AuthorityMode.AGENT
        or run.investigation_id != record.id
    ):
        raise V11ProjectionIntegrityError("Agent projection RuntimeRun is not the owner")

    if active_runtime_id is None:
        raise V11ProjectionIntegrityError(
            "Agent projection lacks active runtime owner"
        )

    latest_run = record.multi_agent_run
    if (
        latest_run is None
        or latest_run.authority_mode != AuthorityMode.AGENT
        or latest_run.runtime_run_id != active_runtime_id
    ):
        raise V11ProjectionIntegrityError(
            "Agent projection latest run summary does not match active owner"
        )
    if review is None or review.authority_mode != AuthorityMode.AGENT:
        raise V11ProjectionIntegrityError(
            "Agent projection coordination review is unavailable"
        )
    if review.investigation_id != record.id:
        raise V11ProjectionIntegrityError(
            "Agent projection coordination review investigation mismatch"
        )
    try:
        validate_v11_final_status(review, latest_run)
    except ValueError as exc:
        raise V11ProjectionIntegrityError(
            "Agent projection final status contract failed"
        ) from exc

    if record.report is not None and record.report.authority_mode == AuthorityMode.AGENT:
        if record.report.investigation_id != record.id:
            raise V11ProjectionIntegrityError("Agent report investigation mismatch")
        if record.report.diagnostic_status != review.diagnostic_status:
            raise V11ProjectionIntegrityError("Agent report diagnostic status mismatch")

    contract = run.execution_contract
    try:
        if contract.get("execution_contract_digest") != execution_contract_digest(contract):
            raise ValueError("digest")
        validate_v11_execution_contract(contract)
    except (AttributeError, TypeError, ValueError) as exc:
        raise V11ProjectionIntegrityError("Agent projection contract integrity failed") from exc
