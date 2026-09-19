# DiagOps V12 实施计划

设计日期：2026-09-12；验证更新：2026-09-14。第 1–4 步已实现，第 6 步工程检查已完成；第 5 步真实评测待接入模型端点与数据。

设计和验收要求见 [V12 spec](../specs/2026-09-12-diagops-v12-validation-design.md)。

按下面顺序修改，每步先检查已有实现和调用者，复用工作区已有修复，补能复现问题的测试，再运行受影响回归。沿用现有 Runtime、数据库、SDK 和评测脚本，不另建审批或发布流程。

## 1. 修复最终决定与排序

主要位置：`backend/diagnosis/v11_runtime.py`、`backend/diagnosis/result_validation.py`、`backend/domain/runtime.py`、`backend/domain/v11_contracts.py`、CoordinationReview 定义、`backend/services/v11_public.py`、`backend/services/v11_projection.py`、`backend/reports/generator.py`，以及 API、前端和 benchmark 输出路径。

- Critic 在逐候选检查后给 final_decision，明确选择集合与顺序；需要补证时在复审后给终态。Lead 只规划或提前 inconclusive。
- 默认发布节点只保存 Critic 决定，真实调用和确定性记录区分清楚，删除测试替身独有的额外 Lead 裁决行为。
- 报告、API、前端、建议、验证、重放、diff 和评测都按 final_decision 排序，原 rank 仅用于历史／展示。非法、重复、外来或未 accept 候选不能发布。
- 在现有配置 JSON 中增加 diagnosis_contract_revision=2，并纳入已有运行配置检查；缺省按旧版本读取，新格式缺字段不能被当作旧格式。保留旧数据与 lead_decision 所需读取，不新建物理表。
- 更新生产者、持久化、消费者和 fixtures。新代码不能按新含义继续旧 run；无法安全恢复时说明原因并提供关联新诊断。

检查：两个候选展示 A,B、最终决定 B,A，各处必须 B 为 Top1；覆盖子集、空结果、非法 ID、提前停止、旧数据读取和版本不匹配。本地 fake endpoint 通过真实 SDK 验证默认没有 Lead 终态调用。

## 2. 补齐证据与上下文

主要位置：`backend/diagnosis/v11_runtime.py`、`backend/diagnosis/result_validation.py`、`backend/domain/tool_queries.py`、Evidence、现有 Provider 工具适配和 registry，以及 `tests/diagnosis/`、`tests/tools/`。

- 复查已有 Lead 初证、Critic 独立摘要和第二轮 findings 修复；在原 helper 上补覆盖数量和省略提示。
- Lead 摘要最多 6 条，Critic 额外摘要最多 4 条，均每 kind 最多 2 条；必需引用保留，先过滤其他 run 的证据。输入放不进预算时显式失败。
- 第一轮上下文隔离；第二轮只接目标 assessment、缺口和判别信息，不新增候选或第三轮。
- 按 spec 的 HolmesGPT 参考，复查 Investigator 在同一轮内的 SDK 工具循环：读取已提交结果后可调整查询，不把调查轮数当模型请求数。
- 单次结果先过滤、脱敏和限长，标出截断与返回量；累计上下文保留必需引用。已入库细节按现有 evidence_scope／assessment 做限长投影；尚未取到的细节通过原工具支持的时间、实体等过滤条件重新查询。保留角色与同 run 边界，不新增工具、shell 或模型压缩服务。
- 验证工具结果先成为 Evidence，再成为同 task／assessment 的 findings，进入 Critic 复审；意外新候选不得静默丢弃。
- partial 保留最低证据覆盖检查；独立性由 Critic 根据来源解释，不能把 ID／Provider 数量当证明。

检查：未引用反证、同源 span 派生、跨 run 证据、failed 只能解释 gap、摘要省略和明细读取、输入超预算；真实 SDK 替身验证“结果截断→缩小查询→新 Evidence→findings”及第二轮完整工具链。

## 3. 统一预算、重试与恢复

主要位置：`backend/domain/runtime.py`、`backend/config/settings.py`、`backend/diagnosis/v11_runtime.py`、`backend/runtime/`、`backend/services/container.py`、SSE API 和 `tests/runtime/`。

- 按 spec §5 统一工具首次+1、模型首次+3、新契约最多三次纠错且共享额度；SDK／Provider 不另重试，恢复沿用已保存额度且不清零次数。
- 按实际远端请求计模型次数，保留原子预算预留和结算。启动及继续调查前，为活动 Investigator 无工具收尾和后续 Critic／复审保留次数与可行 token 额度；只够收尾时关闭工具，不能启动的补证保留缺口。未知 usage 记为未知且保守扣预算，成功工具复用，模型重放仍计费。
- 新诊断默认模型请求上限从 8 调整为 16，产品 120s、工具 10s、全局 8 次逻辑工具调用不变；同步默认配置、请求校验、fixtures 与评测参数，旧 run 保留保存值，恢复不重置时间和已用预算。16 来自 spec 的路径估算，实际效果仍需测量。
- 复查快速重启后的周期 lease 审计、人工恢复、创建失败终态、旧诊断重跑不改源、失权写入、取消、迟到结果和关闭清理。
- SSE 使用持久化事件续传与去重，明确无效 cursor 和历史缺失。

检查：fake endpoint 通过真实 SDK 跑通三路两轮共 15 次模型请求、6 次工具调用的路径；另测第 16 次余量、第 17 次拒绝、小预算停工具收尾及无法收尾时明确失败。真实 SQLite 验证事务、预算并发、快速重启、恢复、退避超时、纠错不产生第五次尝试、重复提交、终态后返回和 SSE。memory 测试不能代替持久化验证。

## 4. 调整轻量评测脚本

主要位置：`backend/benchmarks/rcaeval/runner.py`、`models.py`、`audit.py`、`evaluator.py`、`__main__.py` 和 `tests/benchmarks/`。

- 复用 OB30、现有 Provider 和输出目录，提供最小的开发评测入口；保留正式 SS15／TT90 路径的原有要求。
- Single 与 Multi-Critic 使用同样数据、模型、工具和总预算上限；Single 自己给最终候选，不能伪造 Critic accept。
- 保存 Multi-Critic 的候选、证据和审查供一次追加 Lead 使用；不给 Lead 最终排序、摘要或展示 rank，不调用新工具，不写回原诊断。
- 输出一份配置记录、逐例结果和汇总。支持、反证和上下文引用区分角色，保留旧预测格式的读取。
- Top1 使用最终顺序；全部 30 个案例进入主分母，单独记录无快照、失败、inconclusive、取消、超时和未知 usage。
- 保留第一轮候选供离线评分，区分未生成匹配候选、审查排除和最终选择错误；记录预算／上下文耗尽及补证跳过原因。匹配标签只能在保存预测后的评分层使用。
- 参数改变或重新调用模型时另存结果，保留原失败，不新增签名、层层 hash 或评测审批程序。

检查：用 fake endpoint 和合成小样本验证排序、引用分母、候选覆盖与裁决错误、补证跳过、改对改错、额外失败、成本和评分；检查答案不进入模型。合成结果只验证脚本，不能算真实准确率。

## 5. 跑 30-case 对照并分析

本轮现改用 DeepSeek 官方 API，明确关闭思考模式，使用 `deepseek-flash` 与 `https://api.deepseek.com/beta`，暂不设金额上限。预检与实际调用使用同一非思考参数；用户已确认下一批 Single 与 Multi 共同采用 60,000 Tokens 上限，保留 120s／16 次模型请求／8 次工具调用，追加 Lead 本次修复验证调整为 12,000 Tokens（原为 4,000）；官方密钥从用户环境变量 `DEEPSEEK_API_KEY` 传入本地评测进程。以下 GPT 批次保留为调试历史：最初已提供网关 API base URL、数据目录和本地密钥。用户脚本 `D:\data\RCAEval\tools\run-v12-ob30.ps1` 首次预检超过 30 秒；仅将预检时限改为 90 秒后，第二批次能力检查通过并采用 strict_output_tool。

第二批次 `D:\data\RCAEval\results\v12-ob30-20260914-094043-725` 的真实预测发现预算结算缺陷，已停止并保留原始产物。保存了 5 份失败预测，另有 1 个请求停止时未返回；已知诊断用量 40,890 Tokens，原预测仅统计 4,088 Tokens，未知请求与预检消耗不包含在前者。标签尚未读取，这批只用于调试，不能报告准确率或 Lead 收益。

已修复结构化收尾 schema 漏算、输入校准只能下调、单次预留不足误拒绝、失败请求 usage 漏统计及预算失败分类。结算仅使用尚未分配的余额补差，保留其他并发请求的预留；当时保留 12,000 Tokens／120s 等每例预算。代码变更后需重新执行原脚本，以新的能力检查和源码记录另存批次。此前读取用户级密钥并启动进程的命令被自动审批拒绝，启动仍由用户在本机执行，无需再次提供密钥或确认费用。

第三批次 `v12-ob30-20260914-143701-202` 在能力预检阶段失败，未产生预测。已修复复合预检共用单次截止时间、未选结构化通道超时阻断备用通道两处问题；增加具体失败角色诊断信息，并修正原生 JSON 探测提示仍要求旧版字段、与 V12 schema 不一致的问题。单次预检请求仍为 90 秒，运行预算不变；能力检查与开发评测模块 28 passed，末次原生提示／schema 一致性检查 1 passed（重叠），Ruff 和 diff 检查通过；产物保存于 `output/v12-validation/20260914-capability-deadline/`。未再次调用真实端点或重跑全量、前端测试。

第四批次 `D:\data\RCAEval\results\v12-ob30-20260914-145420-921` 仍未启动预测。原生 JSON schema 已通过并被选用，备用 strict_output_tool 超时未计入阻断项；三路并发检查及 LeadAdjudicationOutput、FinalDecisionDraft、V11SingleControlOutput 请求超时，能力检查按预期拒绝。另一次无凭据连接检查 DNS 约 415ms、HTTPS 在约 1,820ms 返回 401，只证明基础连接可达，不证明带凭据模型请求稳定。当前证据不足以区分网关排队、账号并发限制或上游模型延迟；需核查服务端请求日志／并发额度后继续。未修改预算或放宽能力要求，未再次发出真实模型请求。

DeepSeek 批次 `D:\data\RCAEval\results\v12-ob30-deepseek-20260914-155114-881` 能力检查通过，采用 strict_output_tool；原生 JSON schema 不支持是可选通道结果，不阻断。预测反复失败后已停止，保留 6 份失败预测、0 份成功预测；已知诊断消耗 44,174 Tokens，另有 2 次未知 usage 的失败请求与 1 次停止时未结算请求，不含预检消耗。旧记录缺少的实际 usage 无法补回。12,000 Tokens 曾使 Multi 在调查开始前耗尽额度，因此用户确认下一批两组共同提高至 60,000；旧批次不并入新配置的效果统计。

已修复 strict 工具响应解包后，续接历史未重新封装的问题，并使预算估算采用实际发送的封装形态；畸形 envelope 改为结构化输出失败，保留已返回 usage。Lead 单次纠错提示与允许的 inconclusive／可空字段保持一致，并记录有界字段错误码，不保存原始响应。旧 Lead 失败的具体字段不可恢复，是否解决真实失败仍待新批次验证。可选通道不支持现在显示 UNAVAILABLE，能力判定要求不变。相关回归 226 passed，Ruff 与 diff 检查通过；日志及停止批次用量核对位于 `output/v12-validation/20260914-deepseek-runtime/`。未重跑全量／前端测试，未再次调用真实模型。用户用原本地脚本重新启动，生成与当前源码绑定的新预检及预测结果。

按 spec §6 确定实际模型、数据和费用范围，使用运行时已有模型能力检查即可。缺少真实模型或数据时，前面的实现和离线测试照常完成，效果评测如实记为未完成。

- 记录 OB30 案例清单、数据／代码版本、相关未提交改动、模型与 prompt、预算和重试参数。
- 先通过预算路径测试；按 2026-09-16 用户要求以宽松预算运行 30 个 Single 与 30 个 Multi-Critic：两者每例 200,000 tokens、48 次请求、24 次工具、300s，按 case 交替先后；每份有效 Multi-Critic 快照追加一次 Lead 裁决，上限 60,000 tokens、12 次请求、180s。规划额度合计 13,800,000 tokens、3,240 次远端请求；输入只能估算，远端已发生的超额 usage 单独记录，不能宣称这是账单绝对上限。
- 整批运行后按角色及 Single／Multi 分别统计实际用量、峰值、耗时和失败原因，先确认完成性与 Evidence 完整性，再确定收紧后的预算并复核；不同预算批次分开统计。
- 保存全部预测与失败；先核对支持／反证，再评分。无快照也保留案例，不能从准确率分母中删除。
- 分析 Single 与 Multi-Critic 的完整对照，以及 Lead 改对、改错、顺序变化和增量成本。两个分析复用相同 Multi-Critic 结果，不重新跑上游挑最好结果。
- 汇总正确数／30、候选覆盖、裁决错误、调查限制、状态、引用检查、tokens、调用次数和耗时，注明 OB30 是开发集、相同上限不等于实际等成本，Lead 快照实验不是完整 Multi-Lead 实测。

完成标准：所有预定案例都有结果和正确统计；不要求效果必须提升。重算评分可复用预测，重新运行保留原记录；不足 30 个时说明实际进度，不宣称整组完成。

## 6. 回归与交付

检查 spec 中流程、证据、预算、恢复和评测要求都已落实，同步 API／前端说明、领域术语和相关项目介绍。完成修改后运行：

```powershell
uv run ruff check .
uv run pytest -v
uv run python -m backend.services.runtime_acceptance
npm.cmd --prefix frontend run build
git diff --check
```

必要的局部回归在对应步骤运行；整合后做一次完整检查，只有新改动或失败才重复。不涉及 Tempo 的修改无需新增 Docker 验证。列出未运行项和原因，更新 current.md 的实际进度及结果入口。

工程检查通过、30-case 评测完成后再总结 V12 结果；否则区分“工程已验证”和“效果评测未完成”，不把文档或替身测试当成真实效果证据。

## 实现与评测入口

最终决定使用 `FinalDiagnosisDecision`，默认 Critic 明确选择、排序或放弃，发布节点不再调用 Lead。SQLite／报告／API／前端／benchmark 共享最终顺序；历史记录保留读取，新执行使用 revision 2。恢复、未知 usage 扣账及第二轮工具剩余额度已修正。DeepSeek 官方请求在适配器与预检共用非思考设置，已有诊断 JSON 输出和 Evidence 契约保持不变；新评测配置记录 request_extra_body，保留此前模型结果。

开发对照入口为 `backend.benchmarks.rcaeval.dev`。预测命令不接收标签；评分命令只读保存结果，要求先填写支持／反证检查的检查人、判断与理由。正式 SS15／TT90 入口保持原要求。以下路径为占位，接入真实资源后替换：

```powershell
uv run python -m backend.services.model_capability --base-url https://api.deepseek.com/beta --model deepseek-flash --output-dir output/model_capability
uv run python -m backend.benchmarks.rcaeval.dev predict --runtime <OB30_RUNTIME_PACKAGE> --output <NEW_OUTPUT_DIR> --base-url https://api.deepseek.com/beta --capability-artifact <CAPABILITY_JSON> --model deepseek-flash
uv run python -m backend.benchmarks.rcaeval.dev score --predictions <OUTPUT_DIR> --labels-ob30 <OB30_ONLY_LABELS_JSON> --output <NEW_SUMMARY_JSON>
```

凭据由现有环境变量提供，不写进命令或结果。输出保存配置、源码、SQLite、逐例预测／失败、审查、引用核对表、模型事件与 Lead 快照增量成本；评分保留全部 30 个案例作为主分母。合成测试结果仅用于验证统计逻辑。

## 工程验证结果

最终全量回归完成于 2026-09-13，中断后于 2026-09-14 核对产物并收尾。全量运行启动后的末次工具重放、前端修改另以相关回归验证，不把这几组数量相加当作独立测试总数。

| 检查 | 结果 | 产物 |
| --- | --- | --- |
| `uv run pytest -v` | 2459 passed、4 skipped；无失败 | [完整日志](../../../output/v12-validation/20260913-final/pytest.txt) |
| 末次工具重放、连续取证、评测入口回归 | 63 passed | [回归日志](../../../output/v12-validation/20260913-final/tool-replay-regression.txt) |
| 前端 smoke | 8 passed | [日志](../../../output/v12-validation/20260913-final/frontend-smoke.txt) |
| 本地 compatible endpoint canary | 5 passed | [日志](../../../output/v12-validation/20260913-final/local-canary.txt) |
| Runtime acceptance | 14/14 passed | [结果](../../../output/v12-validation/20260913-final/runtime-acceptance.json) |
| Ruff、前端构建、`git diff --check` | 通过 | `output/v12-validation/20260913-final/` |

4 项跳过：Windows 环境的 POSIX symlink 语义检查 1 项、仅适用 SQLite 的 memory 参数检查 3 项。保留一个既有 Starlette/httpx 弃用警告。上述工程回归阶段未运行真实 OB30；9 月 14 日的真实预测及中止原因见第 5 步。未新增 Docker/Tempo 集成验证；本次没有修改 Tempo 集成，不扩展该项。未执行 Git 提交、推送或 PR。

已验证三路两轮的 15 次模型请求／6 次工具路径，以及一条读取截断结果后缩小查询的 16 次请求／7 次工具路径；持久化重放返回当前 run 范围内的缓存 Evidence，保留截断提示。真实模型是否能在调整后的 60,000 tokens／120s 内完成、Multi 是否优于 Single、追加 Lead 是否带来收益，仍由第 5 步回答。

9 月 14 日真实运行发现问题后的补充验证：

| 检查 | 结果 | 产物 |
| --- | --- | --- |
| Runtime、预算、RCAEval／OpenRCA 与开发评测模块 | 290 passed | [日志](../../../output/v12-validation/20260914-budget-fix/pytest.txt) |
| 末次角色／评测失败分类 | 4 passed，与前组有重叠 | [日志](../../../output/v12-validation/20260914-budget-fix/failure-category.txt) |
| 本地 SDK canary | 5 passed | [日志](../../../output/v12-validation/20260914-budget-fix/local-canary.txt) |
| Runtime acceptance | 14/14 passed | [结果](../../../output/v12-validation/20260914-budget-fix/runtime-acceptance.json) |
| Ruff、diff 检查 | 通过 | `output/v12-validation/20260914-budget-fix/` |
| 中止批次已知用量核对 | 40,890 Tokens；另有 1 个未结算请求 | [核对结果](../../../output/v12-validation/20260914-budget-fix/stopped-batch-audit.json) |

本次修复未重复全量 pytest 或前端构建；受影响模块已回归，没有新增前端改动。当前结论：**预算缺陷已修复并通过相关检查，效果评测未完成。** 下一步由用户重新启动原脚本，生成独立批次；不重新确认已约定的模型、费用和数据范围。


DeepSeek 切换验证：兼容适配器、DeepSeek 预设、能力检查和开发评测入口共 74 passed，Ruff、启动脚本语法及 diff 检查通过。日志位于 `output/v12-validation/20260914-deepseek/`。未执行真实 DeepSeek 调用；需先配置官方密钥，再由原脚本启动预检和新批次。未重复全量／前端检查，本次无前端修改。
