"""Build hook: the wheel package manifest must bind the final wheel content.

H6: the committed source manifest describes the repository tree; a wheel built
from any other state (or relocated content) would ship a manifest whose digests
do not match the installed bytes. Regenerate the package-scope manifest from
the final ``build_lib`` content so the installed manifest is always the digest
of what the wheel actually carries. Loaded by file location to avoid importing
the ``backend`` package (and its side effects) at build time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_SOURCE_IDENTITY = Path(__file__).parent / "backend" / "services" / "source_identity.py"
_PACKAGE_MANIFEST = Path("backend") / "services" / "diagops-source-manifest.json"


def _load_source_identity_module():
    spec = importlib.util.spec_from_file_location(
        "diagops_build_source_identity", _SOURCE_IDENTITY
    )
    module = importlib.util.module_from_spec(spec)
    # dataclass 处理会经 sys.modules 回查模块，先注册再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _BuildPyWithWheelManifest(build_py):
    def run(self):
        super().run()
        build_root = Path(self.build_lib).resolve()
        module = _load_source_identity_module()
        module.write_source_manifest(
            build_root,
            output=build_root / _PACKAGE_MANIFEST,
            package_scope=True,
        )


setup(cmdclass={"build_py": _BuildPyWithWheelManifest})
