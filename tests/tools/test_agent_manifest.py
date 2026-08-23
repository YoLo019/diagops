"""V11 九工具 manifest 与 ToolSpec.exposure 契约。"""

import pytest

from backend.domain.tool_calls import ToolExposure, ToolSpec
from backend.providers.registry import ProviderRegistry
from backend.tools.provider_tools import build_provider_tool_registry
from backend.tools.registry import ToolRegistry

EXPECTED_AGENT_MANIFEST = (
    "lookup_memory",
    "query_dependencies",
    "query_metrics",
    "query_related_alerts",
    "query_traces",
    "read_deployments",
    "read_logs",
    "read_runtime_state",
    "read_service_catalog",
)


def test_agent_manifest_contains_exactly_the_nine_approved_tools():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    assert registry.agent_manifest() == EXPECTED_AGENT_MANIFEST
    assert len(registry.list_agent_specs()) == 9


def test_query_prometheus_is_internal_but_legacy_view_is_complete():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    assert registry.get("query_prometheus").exposure == ToolExposure.INTERNAL
    names = {spec.name for spec in registry.list_specs()}
    assert "query_prometheus" in names
    assert set(EXPECTED_AGENT_MANIFEST) < names


def test_agent_specs_are_all_read_only_with_schemas():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    for spec in registry.list_agent_specs():
        assert spec.read_only is True
        assert spec.exposure == ToolExposure.AGENT
        assert spec.input_schema["additionalProperties"] is False


def test_scoped_tool_descriptions_name_the_v11_window_fields():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    for name in ("query_traces", "query_related_alerts", "read_runtime_state"):
        description = registry.get(name).description
        assert "window_start" in description
        assert "window_end" in description
        assert "start_time" in description
        assert "end_time" in description


def test_exposure_defaults_to_agent_for_compatibility():
    assert ToolSpec(name="t", description="d").exposure == ToolExposure.AGENT


def test_assert_agent_callable_rechecks_manifest_and_read_only():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    spec = registry.assert_agent_callable("read_logs", registry.agent_manifest())
    assert spec.name == "read_logs"

    with pytest.raises(ValueError, match="not in frozen agent manifest"):
        registry.assert_agent_callable("query_prometheus", registry.agent_manifest())
    with pytest.raises(ValueError, match="unknown tool"):
        registry.assert_agent_callable("not_a_tool", registry.agent_manifest())

    writable = ToolRegistry()
    writable.register(
        ToolSpec(name="write_thing", description="d", read_only=False),
        lambda **kwargs: None,
    )
    manifest = ("write_thing",)
    with pytest.raises(ValueError, match="read-only"):
        writable.assert_agent_callable("write_thing", manifest)
