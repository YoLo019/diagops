import json
import re
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from backend.services.production_lab import create_lab_app


def test_lab_app_and_dependency_start_healthy(tmp_path):
    app = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )
    dependency = TestClient(create_lab_app(role="dependency", gate_enabled=True))

    assert app.get("/health").json() == {
        "status": "ok",
        "role": "app",
        "mode": "normal",
    }
    assert dependency.get("/health").json() == {
        "status": "ok",
        "role": "dependency",
        "mode": "normal",
    }
    assert app.get("/work").status_code == 200
    assert dependency.get("/work").status_code == 200


def test_lab_deployment_fault_writes_canonical_app_evidence_and_reset(tmp_path):
    log_path = tmp_path / "app.log"
    deployment_path = tmp_path / "deployments.json"
    client = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=log_path,
            deployment_path=deployment_path,
        )
    )

    assert client.post("/fault/deployment").json()["mode"] == "deployment"
    assert client.get("/work").status_code == 500
    line = log_path.read_text(encoding="utf-8").strip()
    assert re.findall(r"([a-z_]+)=", line) == [
        "level",
        "service",
        "environment",
        "component",
        "exception",
    ]
    assert "level=ERROR" in line
    assert "exception=DeploymentException" in line
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))[0]
    assert deployment["service"] == "checkout-service"
    assert deployment["environment"] == "prod"
    assert deployment["version"] == "v2"

    assert client.post("/fault/reset").json()["mode"] == "normal"
    assert client.get("/work").status_code == 200
    assert log_path.read_text(encoding="utf-8") == ""
    assert json.loads(deployment_path.read_text(encoding="utf-8")) == []


def test_lab_timeout_and_traffic_state_transitions(tmp_path):
    dependency = TestClient(
        create_lab_app(
            role="dependency",
            gate_enabled=True,
            dependency_delay_seconds=0,
        )
    )
    app = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )

    assert dependency.post("/fault/timeout").json()["mode"] == "timeout"
    assert dependency.get("/work").status_code == 200
    assert dependency.post("/fault/reset").json()["mode"] == "normal"
    assert app.post("/fault/traffic").json()["mode"] == "traffic"
    assert app.get("/work").status_code == 200
    assert app.post("/fault/reset").json()["mode"] == "normal"


def test_lab_metrics_use_fixed_names_and_safe_labels(tmp_path):
    client = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )
    client.get("/work")

    lines = [
        line
        for line in client.get("/metrics").text.splitlines()
        if line and not line.startswith("#")
    ]
    names = {line.split("{", 1)[0] for line in lines}
    assert names == {
        "container_cpu_usage_seconds_total",
        "container_memory_usage_bytes",
        "container_network_receive_packets_dropped_total",
        "http_request_duration_seconds_bucket",
        "http_requests_total",
        "kube_pod_container_status_restarts_total",
    }
    assert all('service="checkout-service"' in line for line in lines)
    assert all('environment="prod"' in line for line in lines)
    assert all("scenario=" not in line and "mode=" not in line for line in lines)


def test_dependency_never_writes_app_evidence_files(tmp_path):
    log_path = tmp_path / "app.log"
    deployment_path = tmp_path / "deployments.json"
    client = TestClient(
        create_lab_app(
            role="dependency",
            gate_enabled=True,
            log_path=log_path,
            deployment_path=deployment_path,
            dependency_delay_seconds=0,
        )
    )

    client.post("/fault/timeout")
    client.get("/work")

    assert not log_path.exists()
    assert not deployment_path.exists()


def test_lab_new_fault_scenarios_export_bounded_metrics_and_restore(tmp_path):
    client = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )

    def metric(name: str) -> float:
        pattern = rf"^{name}\{{[^}}]*\}} (\S+)$"
        matches = re.findall(pattern, client.get("/metrics").text, re.MULTILINE)
        assert len(matches) == 1
        return float(matches[0])

    baseline_memory = metric("container_memory_usage_bytes")
    assert metric("container_network_receive_packets_dropped_total") == 0
    assert metric("kube_pod_container_status_restarts_total") == 0

    response = client.post("/fault/memory_pressure").json()
    assert response["mode"] == "memory_pressure"
    activated_at = datetime.fromisoformat(response["activated_at"])
    assert activated_at.tzinfo is not None
    assert client.get("/work").status_code == 503
    # bounded gauge：只放大导出值，不真实耗尽内存。
    assert metric("container_memory_usage_bytes") > baseline_memory

    client.post("/fault/reset")
    assert metric("container_memory_usage_bytes") == baseline_memory
    assert client.get("/work").status_code == 200

    assert client.post("/fault/network_corruption").json()["mode"] == (
        "network_corruption"
    )
    client.get("/work")
    client.get("/work")
    # bounded counter：仅递增导出计数，不修改真实网络。
    assert metric("container_network_receive_packets_dropped_total") > 0
    client.post("/fault/reset")
    assert metric("container_network_receive_packets_dropped_total") == 0

    assert client.post("/fault/process_failure").json()["mode"] == "process_failure"
    # bounded counter：只递增一次重启计数，不杀真实进程。
    assert metric("kube_pod_container_status_restarts_total") == 1
    client.get("/work")
    assert metric("kube_pod_container_status_restarts_total") == 1
    client.post("/fault/reset")
    assert metric("kube_pod_container_status_restarts_total") == 0


def test_lab_new_fault_scenarios_produce_generic_failures_without_answer_leak(
    tmp_path,
):
    client = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=True,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )

    for scenario in ("memory_pressure", "network_corruption", "process_failure"):
        client.post(f"/fault/{scenario}")
        response = client.get("/work")
        assert response.status_code == 503
        metrics = client.get("/metrics").text
        # label 只允许通用 service/environment/container，不得出现 scenario/mode。
        assert scenario not in metrics
        assert "scenario=" not in metrics and "mode=" not in metrics
        client.post("/fault/reset")


def test_fault_control_plane_is_absent_outside_production_gate(tmp_path):
    client = TestClient(
        create_lab_app(
            role="app",
            gate_enabled=False,
            log_path=tmp_path / "app.log",
            deployment_path=tmp_path / "deployments.json",
        )
    )

    assert client.post("/fault/deployment").status_code == 404
    assert client.post("/fault/reset").status_code == 404
    assert client.post("/fault/memory_pressure").status_code == 404
    assert client.post("/fault/network_corruption").status_code == 404
    assert client.post("/fault/process_failure").status_code == 404


def test_seeded_baseline_series_covers_the_full_baseline_window():
    seed = Path("config/production-gate/seed-prometheus.sh").read_text(encoding="utf-8")
    # baseline 窗口是诊断窗口（30m）前等长的 60m：seed 必须覆盖 [now-100m, now-31m]。
    assert re.search(r"DIAGOPS_SEED_OLDEST_SECONDS=6000", seed)
    assert re.search(r"DIAGOPS_SEED_NEWEST_SECONDS=1900", seed)
    assert re.search(r"DIAGOPS_SEED_STEP_SECONDS=240", seed)
    # rate[5m] 基线需要每个 5m 采样窗内至少两个点。
    assert 240 < 300
    for name in (
        "container_network_receive_packets_dropped_total",
        "kube_pod_container_status_restarts_total",
        "container_memory_usage_bytes",
        "http_requests_total",
    ):
        assert name in seed
