# Phase 1A 复用调研

调研日期：2026-09-07。

| 功能 | QA Brain 需求 | Upstream | Commit | License | Decision |
|---|---|---|---|---|---|
| Typed Models | 来源与知识契约 | `maxim-shabanov/ai-qa-orchestrator` | `3ce5a32f2ff8f6357aead61d9da96389d01c5fea` | Apache-2.0 | IDEA_ONLY |
| Deterministic Gate | 冲突与审核状态由程序判定 | `maxim-shabanov/ai-qa-orchestrator` | `3ce5a32f2ff8f6357aead61d9da96389d01c5fea` | Apache-2.0 | IDEA_ONLY |
| Progressive Disclosure | 后续 Skill 拆分方式 | `bermudas/qaspace-skill` | `66934f7c34ea9419759145d1b377fe68c985de9a` | Apache-2.0 | IDEA_ONLY |
| Inventory / Raw Source | 页面版本与本地快照 | 未发现可直接复用且更小的实现 | — | — | BUILD |
| Entity Merge / Conflict | 可追溯合并与冲突输出 | 未发现适合当前 JSON 契约的独立组件 | — | — | BUILD |

## 结论

`ai-qa-orchestrator` 的 Pydantic、LangGraph、FastAPI 和 LLM Agent 体系覆盖更大的需求分析流水线；Phase 1A 引入这些依赖会扩大部署与维护面。本阶段只借鉴“类型契约 + 确定性判定”的设计，使用标准库 `dataclass` 和 JSON。

`qaspace-skill` 的 `SKILL.md → references/ → scripts/` 渐进式结构适合后续 QA Brain Skill，但本阶段交付的是 Core，暂不复制 Skill 内容。

本阶段没有复制或改造第三方源码，因此无第三方运行时依赖和 NOTICE 合并要求。

来源：

- <https://github.com/maxim-shabanov/ai-qa-orchestrator/tree/3ce5a32f2ff8f6357aead61d9da96389d01c5fea>
- <https://github.com/bermudas/qaspace-skill/tree/66934f7c34ea9419759145d1b377fe68c985de9a>
