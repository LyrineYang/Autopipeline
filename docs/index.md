# 文档导航

首次使用从 [README](../README.md) 开始。产品操作、agent 协议和研究设计分别阅读，
无需先理解整个 tree runtime 才能运行一个模型。

## 用户路径

| 你现在需要什么 | 入口 |
| --- | --- |
| 安装并完成第一次 CPU 评估 | [README 快速开始](../README.md#快速开始) |
| wheel / 现有 PyTorch 环境安装 | [安装方式](quickstart.md#安装方式) |
| 配置真实 coding agent | [配置 agent](quickstart.md#配置-agent) |
| 读懂成功、未改进或失败 | [结果与排错](quickstart.md#结果与排错) |
| 导出源码并重新验证 | [导出与复验](quickstart.md#导出复验与重试) |
| 准备模型与代表性输入 | [案例](../examples/README.md) |
| 开始第一次 CUDA 验证 | [服务器使用](quickstart.md#首次服务器使用) |

## 接入与实现

- [架构](architecture.md)：产品、task、评测与独立 benchmark 的职责。
- [Tree Adapter](tree_adapter.md)：兼容工具链、树搜索入口与输出。
- [验证规则](verifier.md)：候选判定及证据。
- [Sensing](sensing.md)：运行中主动探测与证据收集。
- [Agent 入口](agent_entry.md) 与 [运行契约](agent_contract.md)：对接 executor / planner / reflector。
- [Agent 设计背景](agent_design_context.md)：实现边界与常见误读。

## 维护与研究

- [贡献指南](../CONTRIBUTING.md)、[发布检查](dev/open_source_readiness.md)、[更新记录](../CHANGELOG.md)。
- [维护与历史设计索引](dev/README.md)：设计文档保留；历史设想不作为当前功能承诺。
- [本地分支与工作目录](dev/worktree-map.md)：维护者机器的快照，不是安装要求。
- [AutoKernel 指令](../program.md)、[KernelBench 指令](../kernelbench/program_kb.md)：对应旧工作流。

业务问题附可复现命令与脱敏日志；安全问题按[安全说明](../SECURITY.md)私下报告。
