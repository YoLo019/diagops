from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import ValidationError

from backend.benchmarks.openrca.models import OpenRcaBenchmarkSummary
from backend.services.container import get_container

router = APIRouter(prefix="/benchmarks/openrca", tags=["benchmarks"])

ALLOWED_ARTIFACTS = frozenset(
    {
        "run-manifest.json",
        "fixed-predictions.csv",
        "adaptive-predictions.csv",
        "official-report.csv",
        "summary.json",
    }
)


@dataclass(frozen=True)
class _FrozenRun:
    directory: Path
    completed_at: datetime
    summary: OpenRcaBenchmarkSummary


@router.get("/latest", response_model=OpenRcaBenchmarkSummary)
def get_latest_openrca_summary() -> OpenRcaBenchmarkSummary:
    return _latest_frozen_run().summary


@router.get("/latest/{artifact_name}", response_class=FileResponse)
def download_latest_openrca_artifact(artifact_name: str) -> FileResponse:
    if artifact_name not in ALLOWED_ARTIFACTS:
        raise HTTPException(status_code=404, detail="benchmark artifact not found")
    run = _latest_frozen_run()
    artifact = (run.directory / artifact_name).resolve()
    if not artifact.is_relative_to(run.directory) or not artifact.is_file():
        raise HTTPException(status_code=404, detail="benchmark artifact not found")
    return FileResponse(artifact, filename=artifact_name)


def _latest_frozen_run() -> _FrozenRun:
    root = get_container().settings.benchmark.results_path.resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="no frozen OpenRCA benchmark run")
    runs = [
        run
        for directory in root.iterdir()
        if directory.is_dir()
        and (run := _load_frozen_run(root, directory)) is not None
    ]
    if not runs:
        raise HTTPException(status_code=404, detail="no frozen OpenRCA benchmark run")
    return max(runs, key=lambda item: (item.completed_at, item.summary.run_id))


def _load_frozen_run(root: Path, directory: Path) -> _FrozenRun | None:
    resolved = directory.resolve()
    if not resolved.is_relative_to(root):
        return None
    summary_path = (resolved / "summary.json").resolve()
    manifest_path = (resolved / "run-manifest.json").resolve()
    if (
        not summary_path.is_relative_to(resolved)
        or not manifest_path.is_relative_to(resolved)
        or not summary_path.is_file()
        or not manifest_path.is_file()
    ):
        return None
    try:
        summary = OpenRcaBenchmarkSummary.model_validate_json(
            summary_path.read_text(encoding="utf-8")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        completed_at = datetime.fromisoformat(str(manifest["completed_at"]))
    except (KeyError, OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or completed_at.tzinfo is None
        or summary.run_id != resolved.name
        or manifest.get("run_id") != summary.run_id
        or summary.completed_at != completed_at
    ):
        return None
    return _FrozenRun(resolved, completed_at, summary)
