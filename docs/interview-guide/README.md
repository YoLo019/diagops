# DiagOps 面试学习手册

这套文档面向两类读者：完全没有编程和 Agent 开发经验的人，以及需要在面试中准确讲清项目设计的人。它不假设你知道 SRE、LLM、Agent、API、数据库或异步编程。

> 当前事实边界：仓库处于 V11 迭代，工程实现已推进到受控评测阶段，但正式 SS15/TT90 评测尚未完成，不能声称“V11 已发布”或“准确率已提升”。依据见 [当前迭代状态](../superpowers/current.md)。

## 建议阅读顺序

| 顺序 | 文档 | 读完能回答什么 |
| --- | --- | --- |
| 1 | [零基础认识项目](01-zero-to-project.md) | 这个项目究竟解决什么问题？Agent 是什么？ |
| 2 | [系统架构总览](02-system-architecture.md) | 从告警进来到报告出去，经过哪些层？ |
| 3 | [Agent 设计详解](03-agent-design.md) | Lead、Investigator、Critic 如何分工？为什么不投票？ |
| 4 | [工具调用与证据系统](04-tool-calling-and-evidence.md) | 模型怎样“查数据”？为什么不能随便执行命令？ |
| 5 | [持久化 Runtime](05-durable-runtime.md) | 超时、取消、宕机恢复、重放和并发怎样实现？ |
| 6 | [数据、API 与前端](06-data-api-frontend.md) | 数据存在哪里？后端与页面怎样连接？ |
| 7 | [安全与可靠性](07-safety-reliability.md) | 如何防提示词注入、越权、幻觉和敏感信息泄漏？ |
| 8 | [评测与测试](08-evaluation-and-testing.md) | 怎样证明系统有效，而不是只会演示？ |
| 9 | [代码阅读与演示路线](09-code-reading-and-demo.md) | 面试前怎样读源码、启动项目、演示主链路？ |
| 10 | [面试问答](10-interview-qa.md) | 高频追问如何作答？哪些话不能夸大？ |
| 11 | [术语表与源码地图](11-glossary-and-source-map.md) | 陌生名词是什么意思？某个设计去哪看代码？ |

## 一句话版本

DiagOps 是一个只读的 SRE 事故诊断平台：它让多个有不同职责的 LLM Agent 在严格预算内调用受控的只读工具收集证据，经过质疑和裁决后输出带证据引用的根因候选；确定性代码不替 Agent 猜原因，只负责权限、校验、持久化、超时、恢复和审计。

## 30 秒面试版本

“DiagOps 面向应用服务事故诊断。告警进入 FastAPI 后会形成 Investigation 和持久化 Runtime Run。V11 由 Lead 规划信息缺口，最多三个相互隔离的 Investigator 并行使用九个只读工具收集日志、指标、Trace、发布、依赖等证据，Critic 对每个候选做因果和反证检查，必要时只允许一轮补充调查，最后由 Lead 裁决。所有结论必须引用本次 Run 已持久化的 Evidence ID。确定性层负责工具白名单、参数范围、预算、幂等、超时、取消、Checkpoint、重放和结果结构校验，但不能替换 Agent 的诊断结论。”

## 你应当建立的三个核心认识

1. **模型不是系统。** LLM 只负责需要语义判断的部分；可靠执行依靠普通代码。
2. **工具不是任意能力。** Agent 看到的只是冻结的九个只读函数，不是 Shell、SSH 或 Kubernetes 管理权限。
3. **结论不是一段自由文本。** 候选根因、反证、状态、证据 ID、预算使用和运行轨迹都有结构化契约并持久化。

## 文档依据

本手册以当前源码为主，重点核对了：

- [V11 设计](../superpowers/specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md)
- [应用装配](../../backend/services/container.py)
- [V11 Agent Runtime](../../backend/diagnosis/v11_runtime.py)
- [九工具注册](../../backend/tools/provider_tools.py)
- [持久化阶段](../../backend/runtime/phases.py)
- [运行协调器](../../backend/runtime/coordinator.py)
- [领域模型](../../backend/domain/)
- [数据库结构](../../backend/db/schema.py)
- [前端工作台](../../frontend/src/App.tsx)

文档中的“设计目标”与“已实现行为”会明确区分。遇到 README、历史设计和当前源码表述不同，以当前 V11 源码、当前迭代状态和持久化执行契约为准。
