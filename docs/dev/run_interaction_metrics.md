# Run Interaction Metrics

这份文档记录 run viewer 背后的数据和指标问题。
早期版本只讨论数据契约；当前代码已经有 `tree/viewer.py` 和
`scripts/tree_report.py`，可以生成 JSONL、SVG、HTML 和 Markdown 摘要。

目标是把“agent 与人类交互”定义成一个稳定的数据通路：

```text
agent / evaluator / verifier / budget / archive
  -> normalized run records
  -> human-readable metrics and explanations
  -> optional table / chart / report / UI
```

这里的交互不是聊天窗口。交互的含义是：

- 人类能快速看清 agent 做了什么。
- 人类能判断结果是否真的变好。
- 人类能理解失败、回退和成本。
- 人类能看到下一步为什么值得做。
- agent 也能从同一套结构化结果里改进下一轮 planner 决策。

所以 run viewer 第一阶段应该是数据契约和指标解释层，而不是可视化层。

## External Tracking Lessons

成熟实验系统的共同点不是图表，而是稳定的数据语义。

### MLflow

MLflow Tracking 把一次执行组织成 run。run 记录 metadata、metrics、parameters、artifacts，并支持后续搜索和比较。它强调 backend store 保存 run 元数据，artifact store 保存较大的输出文件。

对我们的启发：

- tree run 需要明确 run metadata、metrics、artifacts 的边界。
- node sandbox、agent logs、benchmark logs 都是 artifacts。
- `search_state.json` 不应是唯一阅读入口；它更像 backend store 的一个快照。

来源：[MLflow Tracking](https://mlflow.org/docs/latest/ml/tracking/)

### W&B

W&B 的基本工作流是创建 run、记录 config、在 loop 里持续 log metrics、保存 artifacts，然后在 dashboard 或 API 里比较 runs。

对我们的启发：

- branch / attempt 需要有 step-like 记录，不只是最终 node 记录。
- config 和 metrics 要分开：beam width、planner handoff mode 是 config；正确性、加速比、基准测试耗时是 metrics。
- interactive dashboard 只是读取层；数据本身要能导出给程序。

来源：[W&B Experiments](https://docs.wandb.ai/guides/track/)

### TensorBoard

TensorBoard scalar 的核心价值是观察关键指标如何随训练过程变化，并比较不同 runs。它也提醒一点：曲线能显示趋势，但实际判断仍然需要看指标是否对应真实目标。

对我们的启发：

- iteration-effect 图应该显示趋势，不应该替代 evaluator verdict。
- hover / zoom 这类 UI 只是阅读便利；核心是每个 step 的指标记录要可靠。
- “best-so-far” 和 “current attempt” 应该分开，否则会掩盖回退和失败。

来源：[TensorBoard Scalars](https://www.tensorflow.org/tensorboard/scalars_and_keras)

### Ray Tune

Ray Tune 把 trial 的中间结果通过 `tune.report()` 送回调度器，`Tuner.fit()` 返回 `ResultGrid`，可取出每个 trial 的 metrics dataframe。它也把 early stopping、trial scheduler、search algorithm 和 metric/mode 绑在一起。

对我们的启发：

- branch 更像 trial，attempt 更像 trial 内部的 report step。
- search controller 需要读取结构化 attempt records，而不是解析图表。
- early stop / prune 的解释应该进入 human report，因为这关系到“为什么没有继续试”。

来源：[Ray Tune Getting Started](https://docs.ray.io/en/latest/tune/getting-started.html)

## Interaction Semantics

人类真正需要的不是原始日志，而是四类问题的答案。

### 1. 发生了什么

需要回答：

- 第几轮、第几个 branch、第几次 attempt。
- planner 选择了哪个 family / idea / variant。
- executor 修改了哪些文件或候选 surface。
- evaluator 跑了什么评测。

这类信息来自：

- `SearchNode.hypothesis`
- `node.planner_context`
- `ExecutionResult`
- command activity / agent logs
- task-specific evaluator logs

### 2. 结果怎么样

需要回答：

- 正确性是否通过。
- 主要目标指标是否改善。
- 改善是相对 parent、baseline，还是 best-so-far。
- 结果是采纳、回退、失败、超时，还是被预算剪枝。

这类信息来自：

- `EvaluationResult.accepted`
- `EvaluationResult.score`
- `EvaluationResult.metrics`
- `tree_events.jsonl`
- `archive/motifs.jsonl`

### 3. 代价是多少

需要回答：

- API 调用量。
- 令牌使用量。
- 推理成本。
- agent wall-clock 时间。
- 基准测试耗时。
- probe 次数和失败次数。

这类信息来自：

- `BudgetLedger.summary()`
- command status
- evaluator raw logs
- sensing probe history

### 4. 为什么这样判断

需要回答：

- 为什么这个 branch 被采纳。
- 为什么另一个 branch 被回退或剪枝。
- 是 correctness 问题、compile 问题、runtime 问题，还是收益不足。
- 当前不确定性是什么。

这类信息来自：

- verifier summary
- `Insight`
- `RunArchive` outcome
- timeout diagnostic
- evaluator stderr / stdout tail

如果 run viewer 不能回答这四类问题，图画得再漂亮也不算完成交互层。

## Existing Data

当前仓库已经有不少基础。

### Search State

`search_state.json` 包含：

- task snapshot
- search config
- root memory
- frontier node ids
- nodes
- final result
- budget summary
- archive paths

适合回答：

- 当前 best node 是谁。
- 每个 node 的状态和 evaluation summary。
- tree 最终保留了哪些 frontier。
- 总成本是多少。

不适合直接回答：

- branch-local loop 里每一次 attempt 的变化。
- 每一次 benchmark 的耗时和 raw metric 变化。
- 被 revert 的中间候选为什么失败。

### Event Log

`tree_events.jsonl` 包含：

- baseline completed
- proposal completed
- execution completed
- evaluation completed
- reflection completed
- candidate completed
- frontier updated
- search finished

适合回答：

- 时间顺序。
- 一轮里发生了哪些动作。
- 某个 node 的执行和评测事件。

当前状态：

- event payload 已经包含 baseline / proposal / execution / evaluation / reflection / candidate / frontier / finished 等关键事件。
- branch-local iteration 已经通过 `local_iteration` 进入 evaluation event，并由 `tree/viewer.py` 归一化成 attempt。
- viewer 仍然要在缺字段时回看 node snapshot，所以 event payload 的长期稳定性还需要继续维护。

### Run Archive

当前 `RunArchive` 已经写：

- `archive/motifs.jsonl`
- `archive/family_summary.json`
- `archive/planner_prior.json`

适合回答：

- 哪个 family 尝试了几次。
- 哪些 motif 被采纳。
- 哪些 family 应该 cooldown。

需要补强：

- motif 是 node-level 经验，不是 attempt-level 时间序列。
- `before` / `after` 只保留少量 metrics。
- 成本是累计 run cost，不是 node-local cost。

### Command Diagnostics

`tree/command.py` 已经产生：

- `command_status.json`
- `activity.jsonl`
- `timeout_diagnostic.json`
- `env_redacted.json`
- stdout / stderr / output

适合回答：

- agent 是否超时。
- 是否 missing output / invalid output。
- 最后活动是什么。
- role config 是什么。

需要补强：

- command-level cost 要能映射到 node / attempt。
- activity 里的 agent API heartbeat 还依赖外部 command 主动写。

## Attempt Records

`tree/viewer.py` 现在会从 `tree_events.jsonl` 和 `search_state.json` 生成 attempt record。
要让人类清晰理解优化过程，后续重点是继续稳定 attempt 字段，而不是从零开始补这个层。

### Why Node-Level Is Not Enough

一个 node 可能包含多次 local iteration：

```text
branch node
  iteration 1 -> accepted, score improves
  iteration 2 -> rejected, restore best candidate
  iteration 3 -> accepted, score improves again
```

如果只看最终 node：

- 人类看不到中间失败。
- planner 看不到哪个 variant 被 revert。
- iteration-effect 图只能画 node 点，不能画真实实验点。
- diminishing returns 会被低估或高估。

因此应该把 attempt 作为第一等数据单元。

## Data Unit Model

建议稳定使用下面六个层级。

### Run

一次完整 tree search。

字段：

- run id / run dir
- task type
- objective
- target
- config
- hardware summary
- start / end time
- final result

人类含义：

- 这次优化任务是什么。
- 跑了多久。
- 最终有没有得到可用结果。

### Round

一次 frontier expansion。

字段：

- round index
- active parent nodes
- proposed branches
- accepted nodes
- retained frontier

人类含义：

- 这一轮从哪里继续探索。
- 搜索宽度是多少。
- 哪些 branch 被保留。

### Branch

一个 planner 冻结的方向。

字段：

- node id
- parent id
- family
- idea id
- implementation variant
- scope / target
- frozen decisions
- final branch outcome

人类含义：

- agent 这次想试什么方向。
- 这个方向最后是否值得继续。

### Attempt

一次 evaluator 可观察的实验尝试。

字段：

- attempt index
- node id
- local iteration
- candidate path
- changed files
- evaluation metrics
- accepted / rejected
- kept / reverted
- failure class
- cost delta

人类含义：

- 这就是图里的一个实验点。
- 它可能被采纳，也可能被回退，但都应该被记录。

### Event

运行时动作记录。

字段：

- timestamp
- event type
- role
- node / attempt
- payload

人类含义：

- 用来追溯发生顺序。
- 不直接作为主阅读模型。

### Artifact

大文件或辅助证据。

字段：

- kind
- path
- owner node / attempt
- summary

人类含义：

- 需要深入 debug 时再打开。
- 不应该被日常摘要直接展开。

## Metric Taxonomy

run viewer 至少要区分六类指标。

### 1. Gate Metrics

Gate metric 决定结果是否可被采纳。

例子：

- 正确性
- 编译是否通过
- runtime 是否成功
- 输出误差是否在容忍范围内

原则：

- gate metric 通常不参与“越大越好”的排序。
- gate 不通过时，性能指标只能作为诊断，不能作为成功。
- 人类摘要里应该先报告 gate，再报告性能。

推荐中文呈现：

- `正确性`
- `编译状态`
- `运行状态`
- `误差范围`

### 2. Objective Metrics

Objective metric 是 evaluator 用来排序的主要目标。

AutoKernel 当前主要是：

- `speedup_vs_pytorch`

KernelBench 当前主要是：

- `speedup`

Pipeline / Level 5 更可能是：

- `end_to_end_ms`
- `throughput`
- `tokens_per_second`
- `cost_per_token`

原则：

- 一个 run 应该有一个 primary objective metric。
- 如果指标方向是 lower-is-better，要在 display 层明确。
- score 可以是归一化后的内部值，但用户要看到原始业务含义。

推荐中文呈现：

- `加速比`
- `端到端延迟`
- `吞吐量`
- `每 token 成本`

### 3. Display Metrics

Display metric 是给人看的指标，不一定等于 score。

例子：

- AutoKernel 可以用 `吞吐量 TFLOPS` 作主图 y 轴，同时在标签里显示 `加速比`。
- KernelBench 可以直接显示 `加速比`。
- Pipeline 可以显示 `端到端延迟`，但图上要说明越低越好。

原则：

- display metric 必须贴近人类对任务成败的直觉。
- 如果 score 是内部变换值，不要把 score 当人类主指标。
- 同一图内不要混用不同单位。

### 4. Progress Metrics

Progress metric 描述搜索是否还在有效推进。

建议指标：

- `当前最好值`
- `相对 baseline 改善`
- `相对 parent 改善`
- `采纳次数`
- `回退次数`
- `失败次数`
- `最近 N 次改善率`
- `最近 N 次平均收益`
- `达到下一次改善所需实验数`

这些指标回答：

- 是否进入 diminishing returns。
- 是否该切换 family。
- 是否值得继续花成本。

### 5. Cost Metrics

Cost metric 描述 agent 优化本身的开销。

建议指标：

- `API 调用量`
- `令牌使用量`
- `推理成本`
- `agent 耗时`
- `基准测试耗时`
- `probe 次数`
- `每次采纳的平均成本`
- `每 1% 改善的成本`

原则：

- 成本要和效果一起看。
- 只看最终加速比会鼓励无限搜索。
- quality-per-cost 是本项目的核心目标之一。

### 6. Search Quality Metrics

Search quality metric 描述 planner 是否提出了有效方向。

建议指标：

- `family 采纳率`
- `family 平均收益`
- `family 编译失败次数`
- `idea_id 重复率`
- `proposal 多样性`
- `cooldown 命中次数`
- `planner 建议与最终采纳的一致性`

这些指标既给人看，也给 planner 用。

## Metric Selection Rules

选择评测指标时，优先级应当这样排。

### Rule 1: Correctness First

任何性能提升都必须被 correctness gate 约束。

用户-facing 报告应该使用：

```text
正确性通过；加速比从 1.12x 提升到 1.31x。
```

而不是：

```text
score 提升到 1.31。
```

### Rule 2: Primary Objective Must Match Task

不同任务的 primary objective 不一样。

| 任务 | Primary objective | Display metric |
| --- | --- | --- |
| AutoKernel 单 kernel | 加速比 | 吞吐量 / 加速比 |
| KernelBench problem | 加速比 | 加速比 |
| KernelBench batch | fast_p / 通过率 | 阈值通过率 |
| Pipeline / Level 5 | 端到端目标 | 端到端延迟 / 吞吐量 / 成本 |

### Rule 3: Separate Local and End-to-End

单 kernel 指标不能自动代表 end-to-end 效果。

报告中必须区分：

- `局部 kernel 加速`
- `端到端速度变化`
- `pipeline 阶段耗时变化`

如果只有局部 harness，就不能声称 pipeline 已优化。

### Rule 4: Track Measurement Confidence

GPU benchmark 有噪声。单次提升很小的时候，用户需要知道不确定性。

建议记录：

- 重复次数
- median / min / p90
- 方差或相对波动
- warmup 次数
- benchmark mode

第一版可以先记录：

- `基准测试耗时`
- `重复次数`
- `稳定性说明`

### Rule 5: Preserve Rejected Attempts

被回退的尝试对人类和 agent 都有价值。

它们能解释：

- 为什么搜索变慢。
- 哪些方向容易 compile fail。
- 哪些 family 看起来合理但收益不足。
- planner 是否重复犯同一个错误。

因此 viewer 不应只展示 kept improvements。

### Rule 6: Normalize for Comparison, Show Original Units

内部可以用统一 score 排序，但人类需要原始单位。

例子：

```text
内部排序分数：1.31
人类指标：加速比 1.31x；吞吐量 187.1 TFLOPS
```

对于 lower-is-better：

```text
内部排序分数：-82.4
人类指标：端到端延迟 82.4 ms，越低越好
```

## Human-Readable Metric Names

报告中优先使用意义，不直接暴露内部 key。

| 内部字段 | 用户呈现 |
| --- | --- |
| `correctness` | 正确性 |
| `speedup_vs_pytorch` | 相对 PyTorch 加速比 |
| `speedup` | 加速比 |
| `throughput_tflops` | 吞吐量 |
| `bandwidth_gb_s` | 带宽 |
| `end_to_end_ms` | 端到端延迟 |
| `tokens_per_second` | token 吞吐量 |
| `cost_per_token` | 每 token 成本 |
| `bench_time_seconds` | 基准测试耗时 |
| `agent_calls` | agent 调用量 |
| `api_calls` | API 调用量 |
| `total_tokens` | 令牌使用量 |
| `cost_usd` | 推理成本 |
| `keep` | 变更被采纳 |
| `revert` | 变更被回退 |

内部 key 可以留在 JSON 里，但 summary、table、report 应使用中文含义。

## Proposed Data Path

第一阶段不需要前端。需要的是一个 normalized read model。

建议输出：

```text
run_dir/view/
  run_summary.json
  branches.jsonl
  attempts.jsonl
  metrics_catalog.json
  human_timeline.jsonl
```

### `run_summary.json`

记录一次 run 的整体状态：

```json
{
  "run_id": "20260501-matmul",
  "task_type": "autokernel",
  "objective": "Optimize kernel.py while preserving correctness.",
  "primary_metric": {
    "key": "speedup_vs_pytorch",
    "label": "相对 PyTorch 加速比",
    "direction": "higher"
  },
  "display_metric": {
    "key": "throughput_tflops",
    "label": "吞吐量",
    "unit": "TFLOPS",
    "direction": "higher"
  },
  "baseline": {
    "score": 1.0,
    "display_value": 18.3
  },
  "best": {
    "node_id": "node-0017",
    "score": 1.31,
    "display_value": 187.1
  },
  "counts": {
    "实验次数": 95,
    "变更被采纳": 40,
    "变更被回退": 46,
    "失败次数": 9
  },
  "cost": {
    "API 调用量": 0,
    "令牌使用量": 0,
    "推理成本": 0.0
  }
}
```

### `branches.jsonl`

每个 branch 一行：

```json
{
  "branch_id": "node-0007",
  "parent_id": "node-0003",
  "round": 2,
  "family": "memory_hierarchy",
  "idea_id": "memory-access",
  "variant": "shared-memory-tiling",
  "target": "kernel.py",
  "status": "completed",
  "outcome": "accepted_improved",
  "attempt_count": 3,
  "final_score": 1.31,
  "human_summary": "共享内存分块方向被采纳，加速比提升到 1.31x。"
}
```

### `attempts.jsonl`

每次 evaluator attempt 一行：

```json
{
  "experiment_index": 17,
  "branch_id": "node-0007",
  "local_iteration": 2,
  "family": "memory_hierarchy",
  "idea_id": "memory-access",
  "variant": "shared-memory-tiling",
  "gate": {
    "正确性": "PASS",
    "编译状态": "通过",
    "运行状态": "通过"
  },
  "metrics": {
    "加速比": 1.31,
    "吞吐量": 187.1,
    "基准测试耗时": 12.4
  },
  "comparison": {
    "相对 baseline 改善": "+31%",
    "相对 parent 改善": "+8%",
    "是否刷新当前最好": true
  },
  "outcome": "变更被采纳",
  "failure_class": "",
  "evidence_paths": [
    "nodes/node-0007/bench.iter-02.stdout.log"
  ]
}
```

这是 iteration-effect 图真正应该读取的数据。图只是 `attempts.jsonl` 的一种渲染。

### `metrics_catalog.json`

记录指标语义，而不是让每个 renderer 猜：

```json
{
  "speedup_vs_pytorch": {
    "label": "相对 PyTorch 加速比",
    "unit": "x",
    "direction": "higher",
    "role": "objective"
  },
  "throughput_tflops": {
    "label": "吞吐量",
    "unit": "TFLOPS",
    "direction": "higher",
    "role": "display"
  },
  "correctness": {
    "label": "正确性",
    "role": "gate"
  }
}
```

### `human_timeline.jsonl`

给人读的事件流：

```json
{
  "kind": "attempt_result",
  "title": "第 17 次实验：共享内存分块",
  "summary": "正确性通过；吞吐量从 172.4 提升到 187.1 TFLOPS；变更被采纳。",
  "details": [
    "family: memory_hierarchy",
    "相对 parent 改善 +8%",
    "基准测试耗时 12.4 秒"
  ],
  "severity": "success"
}
```

这个文件才是交互层的核心：人类可以读，agent 也可以读。

## Metric Choice By Task

### AutoKernel Single Kernel

默认 gate：

- 正确性
- 编译状态
- runtime 状态

默认 primary objective：

- 相对 PyTorch 加速比

推荐 display metrics：

- 吞吐量
- 相对 PyTorch 加速比
- 带宽
- 峰值利用率

不要默认声称：

- 端到端模型加速
- pipeline-level 改善

除非 final verify 或 pipeline evaluator 明确提供。

### KernelBench

默认 gate：

- 正确性
- ModelNew 接口语义
- benchmark runtime 状态

默认 primary objective：

- 加速比

batch summary 可用：

- fast_p 阈值通过率
- Level 内通过问题数
- 平均 / 中位加速比

注意：

- 单 problem 的加速比和 batch fast_p 是不同层级指标。
- 不要在单 problem 图里混入 batch 指标。

### Pipeline / Level 5

默认 gate：

- 正确性或输出一致性
- pipeline runner contract
- stage metrics 是否完整

primary objective 需要由 spec 明确。

常见选择：

- 端到端延迟，越低越好
- 吞吐量，越高越好
- token 吞吐量，越高越好
- 每 token 成本，越低越好

辅助指标：

- stage imbalance
- slowest stage latency
- communication time
- sync time
- overlap ratio
- GPU utilization
- memory headroom

这里最重要的是解释 bottleneck：

```text
不是只说端到端延迟下降，而是说明下降来自哪个 stage、哪个边界、哪个资源压力。
```

## How This Helps Agents

人类交互层也能反过来帮助 agent。

planner 不应该读取截图，也不应该解析自然语言长报告。它应该读取压缩后的 view data：

- 最近 N 次 attempt 的 outcome。
- 哪些 family 有收益。
- 哪些 family 编译失败。
- 哪些 idea 重复且无收益。
- 最近是否进入 diminishing returns。
- 当前预算是否还值得展开更多 branch。

这比只读 `positive_insights` / `negative_insights` 更稳定。

建议把 `run_summary.json` 和 compressed `attempts` 摘要接入 planner context：

```json
{
  "interaction_context": {
    "recent_attempts": [
      {"family": "memory_hierarchy", "outcome": "变更被采纳", "score_delta": 0.08},
      {"family": "instruction_selection", "outcome": "编译失败", "score_delta": null}
    ],
    "diminishing_returns": true,
    "recommended_human_readable_status": "最近 8 次实验只有 1 次刷新最好值，继续同一方向收益下降。"
  }
}
```

这让 planner 的 branch choice 更像“看过实验曲线后做下一步判断”，而不是只看最后一个 best score。

## What Not To Do Yet

不要先做这些：

- 先设计复杂前端。
- 先写 chart-specific 数据结构。
- 让 plot 脚本直接解析所有 raw logs。
- 让 agent 手写最终报告作为唯一事实来源。
- 只展示 kept improvements，隐藏失败和回退。
- 用内部 score 代替用户能理解的指标。

## First Data Milestone

第一阶段完成标准：

1. 从现有 run 产物生成 normalized view data。
2. 每个 attempt 都有一条记录；如果当前代码还拿不到 attempt，就先用 node-level record 填充并标记 `granularity=node`。
3. 每个 metric 有 label、unit、direction、role。
4. 每个 outcome 有中文解释。
5. 每个 human summary 都能追溯到 evidence path。
6. planner 可以读取压缩后的 interaction context。

这一步完成后，再做图会很简单：

```text
iteration-effect chart = attempts.jsonl + metrics_catalog.json
```

前端也会很简单：

```text
run viewer UI = run_summary.json + branches.jsonl + attempts.jsonl + human_timeline.jsonl
```

## Design Conclusion

run viewer 的本质不是图表系统，而是 tree optimization 的交互语义层。

它应该先解决三个问题：

1. 数据是否完整：每次实验、采纳、回退、失败、成本都能被记录。
2. 指标是否清楚：用户看到的是正确性、加速比、吞吐量、端到端延迟、推理成本，而不是内部字段。
3. 判断是否可解释：每个 keep / revert / prune 都有证据路径和简短原因。

等这三件事稳定后，截图里的 iteration-effect 图只是自然产物，不是第一设计目标。
