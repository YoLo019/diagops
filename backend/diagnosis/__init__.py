"""Diagnosis orchestration package.

保持本包 `__init__` 为空：`backend.providers` 依赖其中的 signal_semantics，
而 coordinator 又依赖 providers，任何 eager re-export 都会形成循环导入。
"""
