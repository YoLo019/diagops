"""Trusted prediction worker entry point independent of the caller's cwd."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if len(raw) < 2 or raw[0] != "--source-root":
        raise ValueError("prediction worker requires an explicit --source-root")
    raw_source_root = Path(raw[1]).expanduser()
    worker_path = Path(__file__).expanduser()
    if not raw_source_root.is_absolute() or not worker_path.is_absolute():
        raise ValueError("prediction worker source and entrypoint must be absolute")
    trusted_root = worker_path.parents[3]
    sys.path.insert(0, str(trusted_root))
    from backend.services.source_identity import reject_reparse_path, resolve_source_identity

    reject_reparse_path(worker_path, "prediction worker entrypoint")
    reject_reparse_path(raw_source_root, "prediction worker source root")
    source_root = raw_source_root.resolve()
    worker_path = worker_path.resolve()
    trusted_root = worker_path.parents[3]
    if source_root != trusted_root:
        raise ValueError("prediction worker source root does not match immutable entrypoint")

    resolve_source_identity(source_root)
    expected_worker = source_root / "backend" / "benchmarks" / "rcaeval" / "prediction_worker.py"
    if not expected_worker.is_file() or expected_worker.read_bytes() != worker_path.read_bytes():
        raise ValueError("prediction worker source does not match immutable package")
    from backend.benchmarks.rcaeval.__main__ import _add_prediction_arguments, _predict

    parser = argparse.ArgumentParser(prog="rcaeval-prediction-worker")
    _add_prediction_arguments(parser)
    arguments = parser.parse_args(raw[2:])
    _predict(arguments)


if __name__ == "__main__":
    main()
