# 维护文档

用户从 [README](../../README.md) 和 [快速开始](../quickstart.md) 进入。
当前行为以 [架构](../architecture.md)、[agent 契约](../agent_contract.md)、
[验证规则](../verifier.md) 及实现为准。

## 当前维护入口

- [发布检查](open_source_readiness.md)：库发布、资源需求和后续验证。
- [分支与工作目录](worktree-map.md)：产品与两个独立 benchmark 的边界。
- [整理审查](cleanup-review.md)：2026-09-14 的整理结果、保留边界和验证记录。
- [运行交互指标](run_interaction_metrics.md)：报告指标的设计背景；字段及输出以当前实现为准。

## 历史设计与调研

以下文档保留设计依据，不作为当前功能清单或运行指南。

- [早期 planner 设计](planner_v1.md)
- [tree planner 历史共识](tree_planner_consensus.md)
- [agent 优化经验调研](agentic_optimization_lessons.md)
- [Symphony / app-server 差距分析](symphony_agent_runtime_gap_report.md)

新增稳定用法应合并到对应用户文档；研究记录不要重复定义产品状态或验收入口。
