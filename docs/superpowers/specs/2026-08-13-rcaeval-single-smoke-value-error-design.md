# RCAEval Single Smoke `ValueError` 根因修复设计

## 背景

OB30 `single_intended` smoke 在首次模型调用完成后失败。持久化事件显示
`read_service_catalog` 启动后被取消，最终预测只保留 `ValueError` 类型，没有异常消息
和 traceback。对同一 case 直接调用 `RcaEvalServiceCatalogProvider` 与 ToolRegistry 均成功，
耗时约 0.9 秒，因此不能把 Provider 扫描误判为根因。

## 目标

1. 在不泄露凭据、路径、prompt 或遥测正文的前提下，保留足够的 RCAEval 失败诊断信息。
2. 用一次获授权的真实 smoke 定位准确根因。
3. 为准确根因增加失败测试并实施最小修复。
4. 只有真实 smoke 返回 `completed=true` 时才宣告成功。

## 非目标

- 不改变已认证的 `native_json_schema` transport。
- 不修改冻结的评测预算、拓扑、工具清单或重试策略。
- 不为规避故障提高 timeout，也不增加兼容层或 fallback。

## 设计

### 第一阶段：安全失败诊断

RCAEval runner 捕获异常时，生成有界的诊断记录：异常类型、经过 `redact_text` 处理并
截断的异常消息，以及 traceback 中属于仓库代码的最后一个文件名和行号。诊断信息仅用于
开发 smoke 输出或日志，不加入冻结 `CasePrediction` 契约，也不进入正式 prediction bundle。

现有 `failure_category` 保持稳定，只存异常类型，避免改变 evaluator 聚合语义。

### 第二阶段：根因修复

运行一次获授权的真实 smoke，根据第一阶段记录的准确异常位置写回归测试。修复必须落在
产生异常的共享边界，不对该 case、工具名或模型输出增加特判。若异常来自依赖库契约，则先
使用当前已安装 SDK 支持的原生配置或调用方式；仅当现有依赖无法正确表达契约时才考虑升级。

### 数据流

模型响应进入 Agents SDK，SDK 执行工具并生成结构化输出；任一边界失败时，runner 捕获原始
异常，安全诊断器只提取允许字段并脱敏，然后 smoke 将诊断写到 stderr。修复后同一路径应
完成工具调用、模型后续轮次、结果校验和 runtime 终态提交。

## 错误处理与安全

- 禁止持久化或打印 API Key、Authorization、完整 URL、本机绝对路径、prompt 和遥测正文。
- 诊断消息先经过现有 `redact_text`，再压成单行并限制长度。
- traceback 只输出仓库相对文件名和行号，不输出局部变量。
- smoke 失败必须返回非零退出码；调用方必须在独立 PowerShell 子进程中执行，避免 `throw`
  后继续打印伪成功标记。

## 测试与验收

1. 单元测试先证明当前 runner 丢失异常原因，再验证安全诊断包含异常类型和仓库位置。
2. 测试凭据、URL、本机路径会被脱敏，长消息会被截断。
3. 捕获真实根因后，为该根因增加独立回归测试并完成红绿循环。
4. 运行 RCAEval、V11 runtime 和相关安全测试。
5. 最终执行一次真实 OB30 Single 24k smoke；验收条件为 `completed=true`、run 状态为
   `completed`、至少一次 model/tool 成功、无 `model.failed`、无只读违规。
