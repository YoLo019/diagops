"""prediction / evaluator 进程启动器的支持输入面隔离。

威胁模型（不得越界声称）：本模块保证的是「可信 runtime 的支持输入面」——
启动参数、环境变量、当前目录、Provider root 与 runtime 包 manifest 中不出现
标签或 scorer 定位符，路径穿越与非空校验 fail closed。它不声称、也无法阻止
同一 OS 账户下的恶意进程扫描任意主机路径；同账户的 custodian 双阶段流程属于
本地留置（locally held-out）、标签输入隔离评估，而非第三方盲测。

时序契约：evaluator 启动器只接受「已冻结」的 prediction bundle——调用方必须
提供冻结时记录的 bundle 哈希，启动前逐文件复核，任何冻结后改动都会 fail
closed；标签包只以只读方式打开并记录其 manifest 哈希。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.benchmarks.rcaeval.models import (
    LabelManifest,
    RuntimeManifest,
    canonical_json_sha256,
)
from backend.services.source_identity import reject_reparse_path

# prediction 进程只允许这组最小环境变量穿过；其余环境一律不带入。
PREDICTION_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    # compatible endpoint 凭证只注入 prediction 子进程，不写入任何 artifact。
    "DIAGOPS_AGENTS_API_KEY",
    "RCAEVAL_PREDICTION_CHILD",
)

# evaluator 入口必须留在本包内；生产 runtime/诊断/报告/服务/API 模块一律拒绝。
EVALUATOR_ENTRY_PREFIX = "backend.benchmarks.rcaeval"
FORBIDDEN_EVALUATOR_TOKENS = (
    "backend.diagnosis",
    "backend.runtime",
    "backend.rca",
    "backend.reports",
    "backend.services",
    "backend.api",
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# 路径形态判定：含任一方向分隔符或盘符前缀；普通 flag 值（如 "wait..please"）
# 不属于路径形态，不参与规范化比对，避免把任意字符串当路径 resolve 造成误拒。
_PATH_FORM = re.compile(r"[/\\]|^[A-Za-z]:")


def _looks_like_path(value: str) -> bool:
    return bool(_PATH_FORM.search(value))


def _normalized_path(value: str) -> str | None:
    """resolve + normcase；POSIX 上 normcase 是 no-op，语义不退化。"""
    if not _looks_like_path(value):
        return None
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except OSError:
        return None


def _locator_reaches(target: str, locator: str, *, normalize: bool) -> bool:
    """双层定位符比对：原文子串兜底 + 路径形态输入的规范化比对。

    规范化层堵住盘符大小写与正斜杠变体；非路径形态输入只走原文子串，
    保持修复前的误拒面不扩大。
    """
    if locator in target:
        return True
    if not normalize:
        return False
    norm_target = _normalized_path(target)
    norm_locator = _normalized_path(locator)
    return (
        norm_target is not None
        and norm_locator is not None
        and norm_locator in norm_target
    )


def _reject_argv_traversal(argv: list[str]) -> None:
    """argv 中路径形态元素含 `..` 段即拒；同时按两种分隔符切分以覆盖跨平台形态。

    裸 `..` 元素本身也是上溯路径（不含分隔符，不命中路径形态判定），单独显式拒绝。
    """
    for item in argv:
        if item.strip() == "..":
            raise ValueError(f"path traversal rejected in argv: {item!r}")
        if _looks_like_path(item) and ".." in re.split(r"[/\\]", item):
            raise ValueError(f"path traversal rejected in argv: {item!r}")


@dataclass(frozen=True)
class PredictionLaunchSpec:
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str


@dataclass(frozen=True)
class EvaluatorLaunchSpec:
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    predictions_hash: str = ""
    labels_manifest_hash: str = ""


def build_prediction_launch(
    *,
    runtime_package: Path,
    predictions_dir: Path,
    argv: list[str],
    forbidden_locators: tuple[str, ...] = (),
    env_allowlist: tuple[str, ...] = PREDICTION_ENV_ALLOWLIST,
    provider_roots: tuple[Path, ...] = (),
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> PredictionLaunchSpec:
    """构造 prediction 进程启动规格；任何标签/scorer 定位符或穿越即 fail closed。"""
    runtime_root = _resolve_no_traversal(runtime_package, "runtime package")
    manifest_text = _verify_runtime_package(runtime_root)
    _reject_argv_traversal(argv)
    predictions_root = _resolve_no_traversal(predictions_dir, "predictions directory")
    if _is_within(predictions_root, runtime_root):
        raise ValueError("predictions directory must not live inside the runtime package")

    # 结构化文本（manifest）只做原文子串扫描；路径形态输入另加规范化比对。
    raw_targets = [manifest_text]
    path_targets = list(argv) + [str(predictions_root)]
    env: dict[str, str] = {}
    source_environ = os.environ if environ is None else environ
    for name in env_allowlist:
        if name in source_environ:
            env[name] = source_environ[name]
            path_targets.append(env[name])

    for root in provider_roots:
        resolved_root = _resolve_no_traversal(root, "provider root")
        if not _is_within(resolved_root, runtime_root):
            raise ValueError(f"provider root escapes the runtime package: {root}")
        path_targets.append(str(resolved_root))

    cwd_raw = cwd if cwd is not None else runtime_package
    cwd_resolved = _resolve_no_traversal(cwd_raw, "working directory")
    path_targets.append(str(cwd_resolved))

    # 定位符扫描先于目录归属检查：标签目录即使恰好在某个 root 内也必须先暴露。
    for locator in forbidden_locators:
        if not locator:
            continue
        for target in raw_targets:
            if _locator_reaches(target, locator, normalize=False):
                raise ValueError(f"label/scorer locator reaches prediction input: {locator!r}")
        for target in path_targets:
            if _locator_reaches(target, locator, normalize=True):
                raise ValueError(f"label/scorer locator reaches prediction input: {locator!r}")

    if not (_is_within(cwd_resolved, runtime_root) or _is_within(cwd_resolved, predictions_root)):
        raise ValueError("working directory must stay inside runtime or predictions roots")

    return PredictionLaunchSpec(argv=tuple(argv), env=env, cwd=str(cwd_resolved))


def build_evaluator_launch(
    *,
    predictions_bundle: Path,
    expected_bundle_hash: str,
    label_package: Path,
    argv: list[str],
    expected_runtime_manifest_hash: str,
    expected_label_manifest_hash: str,
    cwd: Path | None = None,
) -> EvaluatorLaunchSpec:
    """构造 evaluator 进程启动规格；只接受已冻结的 prediction bundle。

    `expected_runtime_manifest_hash` 是 custodian 在 prepare 阶段记录的 runtime
    manifest 哈希；标签包内声明的绑定与此不一致说明标签/产物不成对，fail closed。
    """
    if not _SHA256_PATTERN.fullmatch(expected_bundle_hash):
        raise ValueError("expected bundle hash must be a sha256 hex digest")
    if not _SHA256_PATTERN.fullmatch(expected_runtime_manifest_hash):
        raise ValueError("expected runtime manifest hash must be a sha256 hex digest")
    bundle_root = _resolve_no_traversal(predictions_bundle, "predictions bundle")
    sums_path = bundle_root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ValueError("predictions bundle has no SHA256SUMS; refuse un frozen bundle")
    actual_hash = hashlib.sha256(sums_path.read_bytes()).hexdigest()
    if actual_hash != expected_bundle_hash:
        raise ValueError("predictions bundle hash mismatch: refuse post-freeze bundle")
    _verify_checksums(bundle_root)

    labels_root = _resolve_no_traversal(label_package, "label package")
    labels_path = labels_root / "labels.json"
    if not labels_path.is_file():
        raise ValueError("label package has no labels.json")
    if not _SHA256_PATTERN.fullmatch(expected_label_manifest_hash):
        raise ValueError("expected label manifest hash must be a sha256 hex digest")
    labels_manifest_hash = expected_label_manifest_hash

    if not argv or not any(_module_token_in(EVALUATOR_ENTRY_PREFIX, item) for item in argv):
        raise ValueError("evaluator entry must stay inside backend.benchmarks.rcaeval")
    for item in argv:
        for token in FORBIDDEN_EVALUATOR_TOKENS:
            if _module_token_in(token, item):
                raise ValueError(f"evaluator entry may not invoke production runtime: {item!r}")

    cwd_resolved = (
        _resolve_no_traversal(cwd, "working directory") if cwd is not None else bundle_root
    )
    return EvaluatorLaunchSpec(
        argv=tuple(argv),
        env={},
        cwd=str(cwd_resolved),
        predictions_hash=actual_hash,
        labels_manifest_hash=labels_manifest_hash,
    )


def _module_token_in(token: str, item: str) -> bool:
    """模块 token 同时匹配点分与斜杠两种形态，堵住脚本路径形态的绕过。

    比较前先折叠重复分隔符：`backend//runtime//coordinator.py` 在 Windows 与
    POSIX 上都是合法可执行拼写，不能借双分隔符逃避匹配。
    """
    slashed_item = re.sub(r"/+", "/", item.replace("\\", "/"))
    return token in item or token.replace(".", "/") in slashed_item


def _resolve_no_traversal(path: Path | None, what: str) -> Path:
    if path is None:
        raise ValueError(f"{what} is required")
    raw = str(path)
    if ".." in Path(raw).parts:
        raise ValueError(f"path traversal rejected in {what}: {raw!r}")
    reject_reparse_path(Path(raw), what)
    return Path(raw).resolve()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _verify_runtime_package(runtime_root: Path) -> str:
    manifest_path = runtime_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("runtime package has no manifest.json; fail closed")
    manifest_text = manifest_path.read_bytes().decode("utf-8")
    manifest = RuntimeManifest.model_validate(json.loads(manifest_text))
    if _manifest_hash(manifest) != manifest.manifest_hash:
        raise ValueError("runtime manifest hash mismatch: package root validation fails closed")
    _verify_checksums(runtime_root)
    return manifest_text


def verify_runtime_package(runtime_root: Path) -> RuntimeManifest:
    """完整复核 runtime package，并返回已验证 manifest。"""
    resolved = _resolve_no_traversal(runtime_root, "runtime package")
    text = _verify_runtime_package(resolved)
    return RuntimeManifest.model_validate_json(text)


def parse_canonical_checksum_bytes(raw: bytes) -> dict[str, str]:
    """统一严格 SHA256SUMS parser；所有 checksum 消费方共用。

    拒绝 duplicate/missing order/非 canonical 路径拼写（`./`、绝对路径、`..`、
    反斜杠、盘符、大小写别名）与非 canonical 序列化（缺尾换行、混用 CRLF、
    未排序）；返回 {posix 相对路径: digest}。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("checksum manifest is not UTF-8") from exc
    expected: dict[str, str] = {}
    seen_casefold: set[str] = set()
    previous: str | None = None
    lines = text.splitlines(keepends=True)
    if not lines or any(not line.endswith(("\n", "\r\n")) for line in lines):
        raise ValueError("checksum manifest serialization is not canonical")
    newline = "\r\n" if "\r\n" in text else "\n"
    if "\r" in text.replace("\r\n", ""):
        raise ValueError("checksum manifest serialization is not canonical")
    for line in lines:
        content = line[: -len(newline)]
        digest, separator, relative = content.partition("  ")
        if not separator or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError(f"malformed checksum line: {content!r}")
        relative_path = Path(relative)
        if (
            not relative
            or "\\" in relative
            or relative.startswith("/")
            or relative_path.is_absolute()
            or relative_path.drive
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.as_posix() != relative
        ):
            raise ValueError(f"checksum path is not canonical: {relative!r}")
        if previous is not None and relative <= previous:
            raise ValueError("checksum manifest paths must be strictly sorted and unique")
        if relative in expected or relative.casefold() in seen_casefold:
            raise ValueError("checksum manifest contains duplicate or aliased paths")
        previous = relative
        seen_casefold.add(relative.casefold())
        expected[relative] = digest
    canonical = "".join(
        f"{digest}  {relative}{newline}" for relative, digest in sorted(expected.items())
    )
    if raw != canonical.encode("utf-8"):
        raise ValueError("checksum manifest serialization is not canonical")
    return expected


def verify_frozen_prediction_root(
    predictions_root: Path, expected_root_hash: str
) -> dict[str, bytes]:
    """evaluator child 首个副作用前的独立复验：root checksum、canonical
    SHA256SUMS bytes/hash 与全部 bundle bytes。

    返回 {posix 相对路径: 已验证字节}；调用方必须消费这些字节而不是重新读盘，
    从而关闭 verify→parse 之间的替换竞态。任何 post-freeze 改动都零 artifact。
    """
    if not _SHA256_PATTERN.fullmatch(expected_root_hash):
        raise ValueError("expected root hash must be a sha256 hex digest")
    root = _resolve_no_traversal(predictions_root, "prediction root")
    sums_path = root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ValueError("prediction root has no SHA256SUMS; refuse unfrozen root")
    reject_reparse_path(sums_path, "prediction root checksum")
    raw = sums_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_root_hash:
        raise ValueError("prediction root checksum mismatch: refuse post-freeze root")
    entries = parse_canonical_checksum_bytes(raw)
    verified: dict[str, bytes] = {}
    actual: set[str] = set()
    # 边迭代边检查，不物化 sorted()：junction 环下 rglob 递归指数膨胀，
    # 条目一产出即经 reject_reparse_path 拒绝，绝不递归进入。
    for path in root.rglob("*"):
        reject_reparse_path(path, "prediction root entry")
        if path.is_file() and path != sums_path:
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            data = path.read_bytes()
            if (
                relative not in entries
                or hashlib.sha256(data).hexdigest() != entries[relative]
            ):
                raise ValueError(
                    "prediction root bundle changed after freeze; refuse evaluation"
                )
            verified[relative] = data
    if actual != set(entries):
        raise ValueError("prediction root file set differs from frozen checksum")
    return verified


def _verify_checksums(package_root: Path) -> None:
    """按 SHA256SUMS 逐文件复核：缺失、多余或哈希不符一律 fail closed。"""
    reject_reparse_path(package_root, "package root")
    sums_path = package_root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ValueError(f"package has no SHA256SUMS checksum file: {package_root}")
    reject_reparse_path(sums_path, "checksum manifest")
    expected = parse_canonical_checksum_bytes(sums_path.read_bytes())
    actual: dict[str, str] = {}
    # 边迭代边检查，不物化 sorted()：理由同 verify_frozen_prediction_root。
    for path in package_root.rglob("*"):
        reject_reparse_path(path, "package checksum entry")
        if path.is_file() and path != sums_path:
            relative = path.relative_to(package_root).as_posix()
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("package checksum mismatch: files changed after packaging/freeze")


def _manifest_hash(manifest: RuntimeManifest | LabelManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return canonical_json_sha256(payload)
