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

from backend.benchmarks.rcaeval.models import LabelManifest, RuntimeManifest

# prediction 进程只允许这组最小环境变量穿过；其余环境一律不带入。
PREDICTION_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE")

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
    # 标签只读打开：本启动器只读取并记录哈希，不提供任何写路径。
    label_manifest = LabelManifest.model_validate(
        json.loads(labels_path.read_bytes().decode("utf-8"))
    )
    recomputed = _manifest_hash(label_manifest)
    if recomputed != label_manifest.manifest_hash:
        raise ValueError("label manifest hash mismatch: label package fails closed")
    if label_manifest.runtime_manifest_hash != expected_runtime_manifest_hash:
        raise ValueError(
            "label/runtime binding mismatch: label package is not paired "
            "with the expected runtime manifest"
        )

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
        labels_manifest_hash=label_manifest.manifest_hash,
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


def _verify_checksums(package_root: Path) -> None:
    """按 SHA256SUMS 逐文件复核：缺失、多余或哈希不符一律 fail closed。"""
    sums_path = package_root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ValueError(f"package has no SHA256SUMS checksum file: {package_root}")
    expected: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, _, relative = line.partition("  ")
        if not digest or not relative:
            raise ValueError(f"malformed checksum line: {line!r}")
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            raise ValueError(f"checksum path traversal rejected: {relative!r}")
        expected[relative] = digest
    actual: dict[str, str] = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(package_root).as_posix()
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("package checksum mismatch: files changed after packaging/freeze")


def _manifest_hash(manifest: RuntimeManifest | LabelManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
