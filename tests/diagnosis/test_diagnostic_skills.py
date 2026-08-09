"""data-only diagnostic skill catalog 契约（spec 7.6/R24）。"""

import pytest

from backend.diagnosis.diagnostic_skills import (
    DIAGNOSTIC_SKILLS,
    DiagnosticSkill,
    skill_catalog_hash,
    skill_catalog_identity,
    validate_skill_catalog,
)
from backend.providers.registry import ProviderRegistry
from backend.tools.provider_tools import build_provider_tool_registry

EXPECTED_SKILLS = (
    "first_failure_timeline",
    "trace_backtracking",
    "change_and_peer_comparison",
    "causal_falsification",
)


def test_catalog_contains_exactly_the_four_frozen_records():
    assert tuple(skill.name for skill in DIAGNOSTIC_SKILLS) == EXPECTED_SKILLS
    assert all(skill.version == "1.0.0" for skill in DIAGNOSTIC_SKILLS)


def test_required_tools_validate_against_agent_manifest():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    # 不抛异常即通过：四条 skill 的 required_tools 均在九工具 manifest 内。
    validate_skill_catalog(DIAGNOSTIC_SKILLS, registry.list_agent_specs())


def test_required_tools_outside_manifest_are_rejected():
    registry = build_provider_tool_registry(ProviderRegistry([]))
    rogue = DiagnosticSkill(
        name="rogue",
        version="9.9.9",
        when_to_use="never",
        required_tools=("query_prometheus",),
        steps=("step",),
        expected_evidence=("trace_error",),
        stop_conditions=("stop",),
    )

    with pytest.raises(ValueError, match="outside the agent manifest"):
        validate_skill_catalog((*DIAGNOSTIC_SKILLS, rogue), registry.list_agent_specs())


def test_duplicate_skill_names_are_rejected():
    registry = build_provider_tool_registry(ProviderRegistry([]))
    duplicate = DIAGNOSTIC_SKILLS + DIAGNOSTIC_SKILLS[:1]

    with pytest.raises(ValueError, match="unique"):
        validate_skill_catalog(duplicate, registry.list_agent_specs())


def test_catalog_hash_freezes_content_and_order():
    registry = build_provider_tool_registry(ProviderRegistry([]))
    identity = skill_catalog_identity(registry.list_agent_specs())

    assert identity["catalog_hash"] == skill_catalog_hash(DIAGNOSTIC_SKILLS)
    assert identity["skill_names"] == ",".join(
        f"{name}@1.0.0" for name in EXPECTED_SKILLS
    )
    reordered = tuple(reversed(DIAGNOSTIC_SKILLS))
    assert skill_catalog_hash(reordered) != skill_catalog_hash(DIAGNOSTIC_SKILLS)


def test_skill_records_are_data_only():
    for skill in DIAGNOSTIC_SKILLS:
        assert not callable(skill)
        for field_value in (
            skill.when_to_use,
            *skill.steps,
            *skill.expected_evidence,
            *skill.stop_conditions,
        ):
            assert isinstance(field_value, str)
