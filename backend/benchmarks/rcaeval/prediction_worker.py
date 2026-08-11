"""Trusted prediction worker entry point independent of the caller's cwd."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if len(raw) < 2 or raw[0] != "--source-root":
        raise ValueError("prediction worker requires an explicit --source-root")
    source_root = Path(raw[1]).resolve()
    trusted_root = Path(__file__).resolve().parents[3]
    if source_root != trusted_root:
        raise ValueError("prediction worker source root does not match immutable entrypoint")
    sys.path.insert(0, str(source_root))
    from backend.benchmarks.rcaeval.__main__ import _add_prediction_arguments, _predict

    parser = argparse.ArgumentParser(prog="rcaeval-prediction-worker")
    _add_prediction_arguments(parser)
    arguments = parser.parse_args(raw[2:])
    _predict(arguments)


if __name__ == "__main__":
    main()
