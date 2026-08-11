"""Immutable source/package identity independent of the caller's cwd."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

SOURCE_MANIFEST_NAME = "diagops-source-manifest.json"
SOURCE_MANIFEST_SCHEMA = "diagops-source-manifest-v1"
PACKAGE_MANIFEST_RELATIVE = Path("backend") / "services" / SOURCE_MANIFEST_NAME
_PACKAGE_ROOTS = ("backend", "config")
_PACKAGE_FILES = ("pyproject.toml", "uv.lock", "README.md")
_IGNORED_DIRS = {"__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    revision: str
    git_dirty: bool
    manifest_hash: str


def write_source_manifest(
    root: Path, *, output: Path | None = None, package_scope: bool = False
) -> Path:
    """Write the build-time package manifest used by non-Git runtimes."""
    reject_reparse_path(root, "source package root")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("source package root does not exist")
    output = (output or root / SOURCE_MANIFEST_NAME).resolve()
    payload = _manifest_payload(root, exclude=output, package_scope=package_scope)
    sealed = dict(
        payload,
        source_revision=_manifest_hash(payload)[:40],
        manifest_hash=_manifest_hash(payload),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_canonical_json(sealed), encoding="utf-8", newline="\n")
    return output


def resolve_source_identity(root: Path) -> SourceIdentity:
    """Resolve and verify an immutable package manifest or explicit Git root."""
    reject_reparse_path(root, "source package root")
    root = root.resolve()
    manifest = root / SOURCE_MANIFEST_NAME
    if manifest.is_file():
        return _read_manifest(root, manifest)
    if (root / ".git").exists():
        revision = _git_revision(root)
        dirty = _git_dirty(root)
        return SourceIdentity(
            revision=revision,
            git_dirty=dirty,
            manifest_hash=_manifest_hash(_manifest_payload(root)),
        )
    package_manifest = root / PACKAGE_MANIFEST_RELATIVE
    if package_manifest.is_file():
        return _read_manifest(root, package_manifest)
    raise ValueError("immutable source package manifest is required outside Git")


def _read_manifest(root: Path, path: Path) -> SourceIdentity:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("source package manifest is unreadable") from exc
    required = {"schema_version", "scope", "files", "source_revision", "manifest_hash"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("source package manifest schema is not frozen")
    if payload["schema_version"] != SOURCE_MANIFEST_SCHEMA:
        raise ValueError("source package manifest schema is not frozen")
    if payload["scope"] not in {"repository", "package"}:
        raise ValueError("source package manifest scope is invalid")
    files = payload["files"]
    if not isinstance(files, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in files.items()
    ):
        raise ValueError("source package manifest file map is invalid")
    expected = _manifest_payload(
        root,
        exclude=path,
        package_scope=payload["scope"] == "package",
    )
    if payload["scope"] != expected["scope"]:
        raise ValueError("source package manifest scope differs from runtime root")
    if files != expected["files"]:
        raise ValueError("source package manifest file set or digest changed")
    digest = _manifest_hash(expected)
    if payload["manifest_hash"] != digest:
        raise ValueError("source package manifest hash mismatch")
    revision = payload["source_revision"]
    if not isinstance(revision, str) or len(revision) != 40 or any(
        char not in "0123456789abcdef" for char in revision
    ):
        raise ValueError("source package revision is invalid")
    if revision != digest[:40]:
        raise ValueError("source package revision does not match manifest")
    return SourceIdentity(revision=revision, git_dirty=False, manifest_hash=digest)


def _manifest_payload(
    root: Path,
    *,
    exclude: Path | None = None,
    package_scope: bool = False,
) -> dict[str, object]:
    files: dict[str, str] = {}
    excluded = exclude.resolve() if exclude is not None else None
    for path in _iter_package_files(root, package_scope=package_scope):
        if excluded is not None and path.resolve() == excluded:
            continue
        if (
            not package_scope
            and path.relative_to(root).as_posix() == PACKAGE_MANIFEST_RELATIVE.as_posix()
        ):
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("source package contains no immutable files")
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "scope": "package" if package_scope else "repository",
        "files": files,
    }


def _iter_package_files(root: Path, *, package_scope: bool = False):
    reject_reparse_path(root, "source package root")
    paths: list[Path] = []
    package_files = () if package_scope else _PACKAGE_FILES
    package_roots = ("backend",) if package_scope else _PACKAGE_ROOTS
    for relative in package_files:
        path = root / relative
        if path.is_file():
            reject_reparse_path(path, "source package file")
            paths.append(path)
    for relative in package_roots:
        directory = root / relative
        if not directory.exists():
            continue
        reject_reparse_path(directory, "source package directory")
        for path in directory.rglob("*"):
            reject_reparse_path(path, "source package entry")
            if path.is_dir():
                continue
            if any(part in _IGNORED_DIRS for part in path.relative_to(root).parts):
                continue
            if path.suffix in _IGNORED_SUFFIXES:
                continue
            paths.append(path)
    return iter(sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()))


def reject_reparse_path(path: Path, what: str) -> None:
    """Reject symlink/junction/mount traversal before any path is resolved."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    for ancestor in (candidate, *candidate.parents):
        try:
            stat = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"{what} cannot be inspected safely") from exc
        if ancestor.is_symlink() or bool(
            getattr(stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"{what} contains a symlink/junction/reparse point")


def _manifest_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40:
        raise ValueError("Git source revision is unavailable")
    return revision


def _git_dirty(root: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Git source status is unavailable")
    return bool(result.stdout.strip())
