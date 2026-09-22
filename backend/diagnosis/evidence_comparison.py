"""把已展示的指标按实体和比较窗并排呈现，不分类故障或推断因果。"""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime
from typing import Any


def _time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def share_trace_semantics(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """相同的链路解释每页只传一次，避免重复说明挤掉观测；不修改原始证据。"""
    projected = [dict(item) for item in evidence]
    shared = {}
    for field in ("rpc_pair", "child_timing"):
        values = {
            detail["semantics"] for item in projected
            if isinstance(detail := item.get(field), dict)
            and isinstance(detail.get("semantics"), str)
        }
        if len(values) != 1:
            continue
        shared[field] = values.pop()
        for item in projected:
            if isinstance(detail := item.get(field), dict) and "semantics" in detail:
                item[field] = {key: value for key, value in detail.items() if key != "semantics"}
    return {"evidence": projected, **({"trace_semantics": shared} if shared else {})}


def comparison_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """移动有界数值到对照表；不完整范围、超限项及原始细节保留在证据索引。"""
    trace_context = share_trace_semantics(copy.deepcopy(evidence))
    projected = trace_context["evidence"]
    sources = {item["id"]: item for item in projected}
    groups: dict[tuple, dict] = {}
    omitted = 0
    for item in projected:
        if item.get("kind") != "metric_trend":
            continue
        entities = tuple(sorted(set(item.get("scope_entity_ids", []))))
        window = tuple(_time(item.get(key)) for key in ("window_start", "split_at", "window_end"))
        if not entities or not all(window) or not window[0] < window[1] <= window[2]:
            # 未知范围不可假定对齐，也不能借用另一个指标的时间窗。
            continue
        key = (entities, window)
        if key not in groups:
            if len(groups) >= 6:
                omitted += 1
                continue
            groups[key] = {"entity_ids": list(entities), "window_start": window[0],
                           "split_at": window[1], "window_end": window[2], "rows": []}
        group = groups[key]
        profile = item.get("time_profile", [])
        buckets = [[_time(point.get("start")), _time(point.get("end"))] for point in profile
                   if isinstance(point, dict)] if isinstance(profile, list) else []
        means = [point.get("mean") for point in profile
                 if isinstance(point, dict)] if isinstance(profile, list) else []
        comparable_profile = (
            0 < len(buckets) == len(profile) <= 8
            and all(start and end and start <= end for start, end in buckets)
            and all(map(_number, means))
            and ("time_buckets" not in group or group["time_buckets"] == buckets)
        )
        if comparable_profile:
            group["time_buckets"] = buckets
        observations = [{"metric": item.get("metric_name"),
                         "baseline_mean": item.get("baseline_mean"),
                         "observation_mean": item.get("observation_mean")}]
        related = item.get("related_observations", [])
        if isinstance(related, list):
            observations.extend(related)
        complete = isinstance(related, list)
        for index, observation in enumerate(observations):
            if not isinstance(observation, dict):
                complete = False
                continue
            name = observation.get("metric")
            before, after = observation.get("baseline_mean"), observation.get("observation_mean")
            if not isinstance(name, str) or not name or not all(map(_number, (before, after))):
                complete = False
                continue
            # 同名但数值冲突的来源单独保留；相同观测的重复引用不算独立确认。
            basis = ("unit", "value_semantics", "aggregation")
            trend = means if index == 0 and comparable_profile else None
            existing = next((row for row in group["rows"]
                             if row[:3] == [name, before, after]
                             and row[5] == trend
                             and all(sources[row[4][0]].get(field) == item.get(field)
                                     for field in basis)), None)
            if existing is not None:
                if item["id"] not in existing[4]:
                    existing[4].append(item["id"])
                continue
            if len(group["rows"]) >= 32:
                omitted += 1
                complete = False
                continue
            delta = after - before
            group["rows"].append(
                [name, before, after, delta if _number(delta) else None, [item["id"]], trend],
            )
        if complete:
            item.pop("baseline_mean", None)
            item.pop("observation_mean", None)
            if comparable_profile:
                item.pop("time_profile", None)
            # 细项的阈值时间仍保留，不能因移动数值而删除已有观测。
            if related:
                item["related_observations"] = [
                    {key: value for key, value in row.items()
                     if key not in {"baseline_mean", "observation_mean"}}
                    for row in related
                ]
    return {
        **trace_context,
        "metric_comparisons": {
            "columns": ["metric", "baseline_mean", "observation_mean", "absolute_delta",
                        "evidence_ids", "time_profile_means"],
            "groups": [group for group in groups.values() if group["rows"]],
            "omitted_table_entries": omitted,
            "rule": "Rows share entity scope and comparison windows, not proven causal timing. "
                    "time_profile_means aligns with this group's time_buckets; null means "
                    "unavailable, not flat. Compare sustained changes vs isolated bursts, "
                    "absolute changes and normal controls; zero baselines have no finite "
                    "fold change. Units, sampling, status and value semantics remain in the cited "
                    "evidence; missing metadata is unknown. Related metrics do not inherit the "
                    "primary metric's unit. Never compare unlike units as equal "
                    "quantities. Threshold crossings across different metrics are not causal "
                    "onsets: small time offsets do not prove cause/effect order. Unmoved values "
                    "remain in evidence. Repeated citations are not independent observations."
                    + (" RSS growth identifies process memory, not pressure or causality; "
                    "queued requests can also grow RSS. Zero interface drops/errors do not "
                    "exclude added delay or loss outside that interface. These observations "
                    "alone cannot choose between local resource work and an RPC path fault."
                    if any(group["rows"] for group in groups.values()) else ""),
        },
    }
