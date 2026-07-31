from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

_BUCKETS = (0.1, 0.5, 1.0, 5.0)
_SERVICES = {
    "app": "checkout-service",
    "dependency": "payment-service",
}
# bounded 导出值：只放大指标，不真实耗尽内存、修改网络或杀进程。
_NORMAL_MEMORY_BYTES = 52428800
_PRESSURE_MEMORY_BYTES = 419430400
_DROPS_PER_REQUEST = 3


def create_lab_app(
    *,
    role: Literal["app", "dependency"],
    gate_enabled: bool,
    log_path: Path | None = None,
    deployment_path: Path | None = None,
    dependency_url: str | None = None,
    dependency_delay_seconds: float = 2.0,
    dependency_timeout_seconds: float = 0.5,
) -> FastAPI:
    """创建只供 production Gate 使用的最小应用或依赖服务。"""
    if role not in _SERVICES:
        raise ValueError("unsupported production lab role")
    service = _SERVICES[role]
    environment = "prod"
    state = {
        "mode": "normal",
        "requests": {"200": 0, "500": 0, "503": 0, "504": 0},
        "durations": [],
        "memory_bytes": _NORMAL_MEMORY_BYTES,
        "network_drops": 0,
        "process_restarts": 0,
    }
    # 同一容器可能并发接收流量和 Prometheus scrape，锁只保护内存计数快照。
    state_lock = Lock()
    lab = FastAPI(title=f"DiagOps production lab {role}")

    @lab.get("/health")
    def health() -> dict[str, str]:
        with state_lock:
            mode = str(state["mode"])
        return {"status": "ok", "role": role, "mode": mode}

    @lab.get("/work")
    async def work():
        started = time.perf_counter()
        with state_lock:
            mode = str(state["mode"])
        if role == "dependency":
            if mode == "timeout":
                await asyncio.sleep(dependency_delay_seconds)
            _record_request(state, state_lock, "200", time.perf_counter() - started)
            return {"status": "ok", "service": service}

        if mode == "deployment":
            _write_log(
                log_path,
                service=service,
                exception="DeploymentException",
            )
            _record_request(state, state_lock, "500", time.perf_counter() - started)
            return JSONResponse({"status": "error"}, status_code=500)

        if mode in {"memory_pressure", "network_corruption", "process_failure"}:
            # 通用请求失败；故障语义只通过 bounded metric 导出，不写入响应或日志。
            if mode == "network_corruption":
                with state_lock:
                    state["network_drops"] = int(state["network_drops"]) + _DROPS_PER_REQUEST
            _record_request(state, state_lock, "503", time.perf_counter() - started)
            return JSONResponse({"status": "error"}, status_code=503)

        if dependency_url is not None:
            try:
                await asyncio.to_thread(
                    _call_dependency,
                    dependency_url,
                    dependency_timeout_seconds,
                )
            except OSError:
                _write_log(
                    log_path,
                    service=service,
                    dependency=_SERVICES["dependency"],
                    exception="TimeoutException",
                )
                _record_request(
                    state,
                    state_lock,
                    "504",
                    time.perf_counter() - started,
                )
                return JSONResponse({"status": "timeout"}, status_code=504)

        _record_request(state, state_lock, "200", time.perf_counter() - started)
        return {"status": "ok", "service": service}

    @lab.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return _metrics(state, state_lock, service, environment)

    if gate_enabled:

        @lab.post("/fault/{scenario}")
        def set_fault(scenario: str) -> dict[str, str]:
            allowed = (
                {
                    "reset",
                    "deployment",
                    "traffic",
                    "memory_pressure",
                    "network_corruption",
                    "process_failure",
                }
                if role == "app"
                else {"reset", "timeout"}
            )
            if scenario not in allowed:
                raise HTTPException(status_code=404, detail="Unknown lab scenario")
            activated_at = datetime.now(UTC)
            with state_lock:
                state["mode"] = "normal" if scenario == "reset" else scenario
                if scenario == "memory_pressure":
                    state["memory_bytes"] = _PRESSURE_MEMORY_BYTES
                elif scenario == "process_failure":
                    state["process_restarts"] = int(state["process_restarts"]) + 1
                elif scenario == "reset":
                    state["requests"] = {"200": 0, "500": 0, "503": 0, "504": 0}
                    state["durations"] = []
                    state["memory_bytes"] = _NORMAL_MEMORY_BYTES
                    state["network_drops"] = 0
                    state["process_restarts"] = 0
            if role == "app":
                if scenario == "deployment":
                    _write_deployment(deployment_path, service)
                elif scenario == "reset":
                    _reset_evidence(log_path, deployment_path)
            return {
                "mode": str(state["mode"]),
                "activated_at": activated_at.isoformat(),
            }

    return lab


def _record_request(
    state: dict[str, object],
    lock: Lock,
    status: str,
    duration: float,
) -> None:
    with lock:
        requests = state["requests"]
        durations = state["durations"]
        assert isinstance(requests, dict)
        assert isinstance(durations, list)
        requests[status] = int(requests[status]) + 1
        durations.append(duration)


def _metrics(
    state: dict[str, object],
    lock: Lock,
    service: str,
    environment: str,
) -> str:
    with lock:
        requests = dict(state["requests"])
        durations = list(state["durations"])
        memory_bytes = int(state["memory_bytes"])
        network_drops = int(state["network_drops"])
        process_restarts = int(state["process_restarts"])
    labels = f'service="{service}",environment="{environment}"'
    container_labels = f'{labels},container="{service}"'
    lines = [
        "# TYPE http_requests_total counter",
        *[
            f'http_requests_total{{{labels},status="{status}"}} {count}'
            for status, count in requests.items()
        ],
        "# TYPE http_request_duration_seconds_bucket counter",
        *[
            (
                f'http_request_duration_seconds_bucket{{{labels},le="{bucket}"}} '
                f"{sum(value <= bucket for value in durations)}"
            )
            for bucket in _BUCKETS
        ],
        (f'http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {len(durations)}'),
        "# TYPE container_cpu_usage_seconds_total counter",
        f"container_cpu_usage_seconds_total{{{container_labels}}} 0.01",
        "# TYPE container_memory_usage_bytes gauge",
        f"container_memory_usage_bytes{{{container_labels}}} {memory_bytes}",
        "# TYPE container_network_receive_packets_dropped_total counter",
        f"container_network_receive_packets_dropped_total{{{container_labels}}} {network_drops}",
        "# TYPE kube_pod_container_status_restarts_total counter",
        f"kube_pod_container_status_restarts_total{{{container_labels}}} {process_restarts}",
    ]
    return "\n".join(lines) + "\n"


def _call_dependency(url: str, timeout_seconds: float) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        if response.getcode() >= 400:
            raise OSError("dependency failed")


def _write_log(
    path: Path | None,
    *,
    service: str,
    exception: str,
    dependency: str | None = None,
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "level=ERROR",
        f"service={service}",
        "environment=prod",
        f"component={service}",
    ]
    if dependency is not None:
        fields.append(f"dependency={dependency}")
    fields.append(f"exception={exception}")
    line = f"{datetime.now(UTC).isoformat()} {' '.join(fields)}\n"
    with path.open("a", encoding="utf-8") as file:
        file.write(line)


def _write_deployment(path: Path | None, service: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "service": service,
                    "environment": "prod",
                    "version": "v2",
                    "instance": "",
                    "deployed_at": datetime.now(UTC).isoformat(),
                    "operator": "production-gate",
                    "commit": "gate-v2",
                    "summary": "service version v2 deployed",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _reset_evidence(log_path: Path | None, deployment_path: Path | None) -> None:
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
    if deployment_path is not None:
        deployment_path.parent.mkdir(parents=True, exist_ok=True)
        deployment_path.write_text("[]\n", encoding="utf-8")


def _enabled(value: str | None) -> bool:
    return value is not None and value.casefold() == "true"


app = create_lab_app(
    role=os.getenv("DIAGOPS_LAB_ROLE", "app"),
    gate_enabled=_enabled(os.getenv("DIAGOPS_PRODUCTION_GATE")),
    log_path=Path(os.getenv("DIAGOPS_LAB_LOG_PATH", "/evidence/logs/app.log")),
    deployment_path=Path(
        os.getenv(
            "DIAGOPS_LAB_DEPLOYMENT_PATH",
            "/evidence/deployments/deployments.json",
        )
    ),
    dependency_url=os.getenv("DIAGOPS_LAB_DEPENDENCY_URL"),
    dependency_delay_seconds=float(os.getenv("DIAGOPS_LAB_DEPENDENCY_DELAY_SECONDS", "2")),
    dependency_timeout_seconds=float(os.getenv("DIAGOPS_LAB_DEPENDENCY_TIMEOUT_SECONDS", "0.5")),
)
