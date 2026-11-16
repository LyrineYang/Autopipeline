# 开发者入口

这里集中运行时扩展、agent 协议、benchmark 适配、兼容工具和维护资料。
用户从 [README](../../README.md) 和 [快速开始](../quickstart.md) 进入。
当前行为以 [架构](../architecture.md)、[agent 契约](../agent_contract.md)、
[验证规则](../verifier.md) 及实现为准。

## 用户与开发入口的边界

后续功能按[产品工程规范](product-engineering-standard.md)设计与验收：
明确用户入口、公共扩展接口、内部实现和 benchmark 的边界，并检查安装包的实际行为。

默认用户入口只介绍完成任务所需的操作；成本、设备要求、结果含义和错误处理
始终可见。内部协议、合成 smoke、benchmark 适配与发布过程从开发入口访问。
不为了简化帮助而删除功能，不要求普通用户先理解内部架构。

## 开发接口

- [架构](../architecture.md)：任务、搜索、评测与验证边界。
- [Agent 入口](../agent_entry.md)和[运行契约](../agent_contract.md)：executor / planner / reflector 集成。
- [Sensing](../sensing.md)、[验证规则](../verifier.md)：证据收集与候选判定。
- [Tree Adapter](../tree_adapter.md)：旧 kernel 工具与搜索运行时。
- [Agent 设计背景](../agent_design_context.md)：实现约束和常见误读。
- [贡献指南](../../CONTRIBUTING.md)：开发检查与提交边界。
- [运行记录与诊断](run-artifacts.md)：详细日志、报告语义与安装包用户流程验收。

默认帮助只展示产品工作流。完整工具清单：

```sh
autopipeline --help-all
autopipeline pipeline --help-all
```

`pipeline info`、`pipeline setup` 用于 spec 检查与工作区复制；`pipeline smoke`
只检查合成控制流程。它们继续可用，不作为第一次真实模型运行的推荐路径。

## 接入其他 coding agent


用 `--executor-command 'YOUR_COMMAND'` 替换 executor backend。命令在候选工作区
运行，读取 TREE_PROMPT_PATH / TREE_PAYLOAD_PATH，修改声明的文件，并把结果 JSON
写到 TREE_OUTPUT_PATH，例如：

```json
{"status":"completed","changed":true,"summary":"说明实际实现改动"}
```

JSON 是协议输出，不是正确性证据；工具仍独立执行评测。使用能够访问已安装库
的解释器，认证由外部命令负责。完整协议见[Agent Contract](../agent_contract.md)。
`pipeline smoke` 是合成控制流程检查，不替代这里的真实模型执行。

## 兼容工具


以下仅在源码根目录、具备 GPU 依赖与 agent 认证时使用：

```sh
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
uv run autopipeline tree run --kernel-file workspace/kernel_matmul_1.py \
  --beam-width 1 --max-rounds 1 --children-per-expansion 1 \
  --bench-arg=--quick --executor-backend codex-app-server
```

原生 pipeline 不要求先 profile/extract：它的编辑范围是提供的模型实现；旧流程
编辑抽取出的 kernel。更多兼容入口说明见 [Tree Adapter](../tree_adapter.md)。

## 外部 benchmark


它们是独立的测试和统计开发线，不是上述案例的别名，也不作为产品核心的依赖。
产品接收固定的 workspace、editable_paths 和 evaluate_command；benchmark
负责题目版本、输入/输出约束、固定基线、评分、统计和自己的模型资源。

当前 Level 5 harness 使用自己的 active workspace，尚需在其开发线上增加可显式
传入工作区的运行接口，再输出产品验证器所需的数值与性能证据。适配不得改变题目
或评分规则，也不能仅把 benchmark 的一句 PASS 当成完整数值证据。
apimetric 保留在 benchmark 侧统计实际实验，不迁移成产品的另一套预算引擎。

接入顺序：先固定单题的离线调用协议；再验证同一题在独立 harness 与产品调用中
得到一致判定；最后才用 GPU 比较 agent 方法。前两步的接口工作不需要等待服务器。
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
