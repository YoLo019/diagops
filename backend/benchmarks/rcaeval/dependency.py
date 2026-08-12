"""Frozen dependency closure used by the label-side scorer and audit gate."""

from __future__ import annotations

import hashlib
from pathlib import Path


def scorer_dependency_paths() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parent
    return tuple(
        root / name
        for name in (
            "audit.py",
            "dependency.py",
            "evaluator.py",
            "ledger.py",
            "models.py",
        )
    )


def scorer_dependency_hash() -> str:
    digest = hashlib.sha256()
    for path in scorer_dependency_paths():
        if not path.is_file():
            raise ValueError(f"scorer dependency is missing: {path.name}")
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
