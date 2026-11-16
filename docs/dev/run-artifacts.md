# 运行记录与诊断参考

日常使用先执行 `autopipeline report RUN_DIR`，或阅读运行目录中的
`view/summary.md`。[使用指南](../quickstart.md#结果与排错)解释用户结果与重试方式。
这里供定位失败、扩展集成和维护时查询；路径相对于单次运行目录。

| 路径 | 用途 |
| --- | --- |
| result.json | 产品状态、候选、文件哈希与最终复验结论；早期失败可能只有此文件 |
| input/、environment.json | 本次冻结的参考/配置，以及依赖版本 |
| search_state.json、tree_events.jsonl | 搜索过程和预算使用记录；不作为用户必须理解的概念 |
| nodes/*/agent/ | agent 请求、输出、超时与失败诊断 |
| nodes/*/verification/ | 参考/候选测量及 evaluator 标准输出和错误 |
| promoted_pipeline/ | 最终选中代码，也可能是未改变的基线 |
| promoted_pipeline/.autopipeline/ | profiler trace 与算子统计 |
| final_verification/ | 最终独立测量与判定报告 |
| view/ | 从运行记录生成的报告和过程可视化 |

报告呈现产品的最终判定，不自行根据某个成功节点推断交付成功。没有 result.json
的历史 tree 运行继续显示原有过程报告，不补造产品验证状态。部分记录只能解释
已发生的行为；`running` 不是进程存活检测，report 也不是恢复调度的命令。

扩展参数使用 `autopipeline pipeline run --help-all` 查询。用户常用帮助与完整帮助
使用同一解析器；隐藏的选项仍按原名称和默认值接受，不影响旧集成。

维护时可用目标安装环境运行
`python /path/to/source/scripts/check_installed_workflow.py --work-dir /new/directory`。
应从源码目录之外执行；它通过已安装 CLI 验证真实 CPU 评估、报告、导出、复验、
错误候选拒绝与重复目录保护，不调用 agent。逐条命令日志和 acceptance.json 留在
指定目录；此项不代表 CUDA 或真实 agent 优化收益的验证。
