# Agentic Optimization Lessons

> Historical design notes. For current paths and behavior, see [Architecture](../architecture.md).

这份文档把 Hermes 和其他成熟 agent / experiment orchestration 系统里的可借鉴机制，映射到本仓库的 tree framework。

它不是临时实验记录，也不是实现计划文件。它只回答一个设计问题：

> 为了把 AutoKernel / KernelBench / Level 5 的迭代式优化做得更快、更稳、更省，哪些工程机制值得吸收，应该怎样落到当前代码边界里？

重点覆盖四项：

- 并行 branch execution
- role-based model config
- 子任务超时诊断
- run-local motif archive

另外也短评 run viewer、probe availability、pipeline trajectory 这三项，因为它们会影响上面四项的接口形状。

## 当前 Story

本仓库的核心 story 不是“做一个通用聊天 agent”。

更准确的定位是：

- AutoKernel 是当前实现基础。
- KernelBench 是 benchmark 基础，被扩展到 repo-defined Level 5。
- Level 5 关注 composed / stitched workloads，不只是单个 isolated architecture。
- tree framework 把 iterative refinement 做成一个可测量、可比较、可复盘的实验循环。

对应到运行时概念：

- `planner` 不是 executor。它负责把 profile、hardware context、history、sensing evidence 压成高层 branch identity。
- branch 不是普通聊天子任务。它是一个带 sandbox、预算、评价函数、keep/revert 结果的实验 trial。
- executor 是 branch-local 实现者。它在一个冻结方向内做局部修改。
- verifier / evaluator 是裁决面。它们让采纳与回退不依赖 agent 自述。
- archive 不是全局 memory。它是当前 run 或当前实验系列里可审计的经验压缩。

这个 story 与 Hermes 有重叠，但不相同。Hermes 优先解决通用 agent 产品里的 delegation、tool boundary、TUI monitoring 和 persistent personal context；我们优先解决 evaluator-driven optimization loop。

## External References

### Hermes Agent

Hermes 的 `delegate_task` 提供了几个直接可借鉴的机制：

- 子 agent fresh context，只接收 parent 显式传入的 goal 和 context。
- batch delegation 用 thread pool 并行执行。
- 子 agent 有独立 terminal session、受限 toolsets、最大 iteration、模型/provider override。
- `/agents` overlay 展示 delegation tree、成本、token、触达文件和历史。
- timeout 会杀掉 quiet child；zero-call timeout 会写结构化诊断。

这些机制适合借鉴到 branch execution，但不能照搬：

- Hermes delegation 是同步且不耐久的，parent turn 被中断时 child 会取消。
- Hermes child summary 进入 parent context；我们的 branch outcome 必须进入 evaluator / search state。
- Hermes 的并行单位是 agent task；我们的并行单位是 sandboxed candidate trial。

来源：

- [Hermes Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Hermes Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- [Hermes Environments](https://hermes-agent.nousresearch.com/docs/developer-guide/environments/)

### LangGraph

LangGraph 的启发点不是“图写法”，而是 durable execution：

- 每个 step 存 checkpoint。
- 恢复时不从同一行代码继续，而是从合适 step replay。
- 有 side effect 的操作要封装成 task，避免 resume 时重复执行。
- persistence 支撑 human-in-the-loop、time travel、fault tolerance。

映射到我们这里：

- `search_state.json` 和 node sandbox 已经是 checkpoint 雏形。
- 未来 resume 不应该重新跑已完成 benchmark 或已完成 agent call。
- 并行 branch 之后更需要把 branch evaluation 视作可恢复 task。

来源：

- [LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Time Travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)

### AutoGen

AutoGen 的参考价值在 role separation：

- 不同 agent 可以拥有不同 role description 和 model client。
- group chat 有 termination conditions，例如最大消息数、token 用量、timeout、外部停止。
- team 可以保留或 reset state。

映射到我们这里：

- planner / executor / reflector 不应该只是三个 command string。它们应该拥有显式 role config。
- branch-local loop 的 stop conditions 应该能来自 planner，也能被 framework 强制执行。
- role config 应该进入 budget ledger，支持按角色看成本和失败。

来源：

- [AutoGen Teams](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html)
- [AutoGen Termination](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html)

### Ray Tune

Ray Tune 的参考价值在 experiment runner：

- trial 并行由每个 trial 的 resource request 和集群资源共同决定。
- scheduler 可以 early terminate、pause、clone 或调整 trial。
- experiment-level restore 和 trial-level failure handling 分开。

映射到我们这里：

- branch 是 trial。
- `TreeSearchEngine` 是 lightweight scheduler。
- `SearchPolicyHint` 是 planner 对 scheduler 的约束建议。
- resource gate 不应该只看 children count，还要看 GPU / CPU / API budget。

来源：

- [Ray Tune Parallelism and Resources](https://docs.ray.io/en/latest/tune/tutorials/tune-resources.html)
- [Ray Tune Fault Tolerance](https://docs.ray.io/en/latest/tune/tutorials/tune-fault-tolerance.html)
- [Ray Tune Early Stopping](https://docs.ray.io/en/latest/tune/getting-started.html#early-stopping-with-adaptive-successive-halving-ashascheduler)

### DSPy

DSPy 的参考价值在 metric-driven prompt / program optimization：

- optimizer 消费 program、metric、examples。
- MIPRO 类流程会先收集 traces，再基于高分 trajectory 生成 grounded proposals，最后离散搜索。
- SIMBA / GEPA 类方法会从 failure / trajectory 中抽规则或修改建议。

映射到我们这里：

- run-local motif archive 是 planner 的 training examples，不是长期 agent memory。
- motif 只有被 benchmark / verifier 证明过，才应该成为强 prior。
- archive 可以支持 branch family 的 cold-start、cooldown 和复用。

来源：

- [DSPy Optimizers](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/optimizers.md)

## Current Repo Baseline

这份文档最早记录的是“应该吸收哪些机制”。当前代码已经实现了其中一部分，
所以这里先把已实现项和剩余缺口拆开，避免后续开发 agent 继续按旧缺口判断。

### 已实现或已有雏形

- [tree/search.py](../../autopipeline/search/engine.py) 管理 baseline、frontier、branch expansion、pruning、promotion。
- [tree/planner.py](../../autopipeline/search/planner.py) 产出 `PlannerPacket`，里面包括 static prior、hardware context、history context、`RegionGraph`、`BottleneckCard`、`SearchPolicyHint`、Branch Cards。
- [tree/branch_engine.py](../../autopipeline/search/branch_engine.py) 在一个固定 branch identity 下运行短 keep/revert loop。
- [tree/search.py](../../autopipeline/search/engine.py) 已经支持受控 parallel branch execution，并在 agent / probe budget 存在时收紧并行。
- [tree/roles.py](../../autopipeline/search/roles.py) 和 [tree/types.py](../../autopipeline/search/types.py) 已经提供 role-based config，planner / executor / reflector 可以有各自 backend、provider、model、timeout 和 reasoning 配置。
- [tree/agent_backend.py](../../autopipeline/search/agent_backend.py)、[tree/codex_app_server.py](../../autopipeline/search/codex_app_server.py) 和 [tree/codex_session.py](../../autopipeline/search/codex_session.py) 已经提供 Codex app-server session backend，同时保留 legacy command bridge。
- [tree/command.py](../../autopipeline/search/command.py) 给外部 planner / executor / reflector 写 prompt、payload、telemetry、output、stdout、stderr、status，并服务 file-bridge probe 请求。
- [tree/sensing.py](../../autopipeline/search/sensing.py) 提供 passive snapshot、diagnostic probes、probe history、verifier summary。
- [tree/budget.py](../../autopipeline/search/budget.py) 记录 agent 使用量、API 调用量、令牌使用量、推理成本、probe 使用量、family 成败。
- [tree/archive.py](../../autopipeline/search/archive.py) 已经提供 run-local motif archive、family summary 和 planner prior。
- [tree/viewer.py](../../autopipeline/search/viewer.py) 已经能把 tree run 转成 summary、JSONL、SVG 和 HTML dashboard。
- [pipeline/task.py](../../autopipeline/pipeline/task.py) 已经提供 spec-driven generic pipeline workspace task 的雏形。
- [kernelbench/task.py](../../kernelbench/task.py) 已经把 KernelBench problem surface 接到 tree task 协议。

### 仍未完成的缺口

- session / turn 状态还没有完整并入统一 run state 和 viewer；很多运行中信息仍在 per-step logs。
- dynamic tool 已经能作为 app-server transport 调用 sensing，但 capability 面还不够产品化：probe 列表、参数约束、失败原因、预算状态和贡献解释仍需更清晰。
- timeout / stall 诊断已有基础，但还没有形成稳定的 semantic activity taxonomy；仍难一眼区分 provider 卡住、工具卡住、probe 等待、agent 已完成但 wrapper 未产出等情况。
- parallel branch execution 仍是本地受控并行，不是资源感知调度器；GPU benchmark 默认仍应保守串行。
- motif archive 已经存在，但还需要更强的 pipeline bottleneck 语义，才能稳定支持 cross-kernel / stage-level prior。
- generic pipeline task 能跑 workspace evaluator 和 metrics，但还不是 production trace profiler、cross-kernel editor、fusion / layout-propagation engine。

下面各节保留了原始设计推导。已经落地的部分应按当前代码理解；未落地的部分仍可作为后续实现边界和验收思路。

## 1. Parallel Branch Execution

### 目标

让同一轮里互相独立的 branches 并行执行，同时保留现有语义：

- 每个 branch 有独立 sandbox。
- 每个 branch 有独立 agent logs。
- 每个 branch 有独立 benchmark logs。
- keep/revert 仍然发生在 branch-local loop 内。
- frontier update 仍然由 search controller 统一裁决。

并行不是为了“多开 agent 看起来更强”，而是为了提升单位 wall-clock 时间内的有效实验数。

### 推荐形态

先做 local branch pool，不直接上分布式调度。

新增配置建议：

```python
max_parallel_branches: int = 1
branch_resource_policy: str = "conservative"
```

第一版只支持 `ThreadPoolExecutor`，原因是：

- 现有 `BranchEngine.run()` 主要在 subprocess benchmark、external command、文件 IO 上等待。
- 每个 branch 已有 node-local sandbox。
- 实现成本低，能快速验证并行是否真的缩短基准测试耗时。

但需要三个边界：

1. search state merge 必须串行。
2. budget ledger 更新必须加锁。
3. task 对象如果有 mutable cache，只能读共享状态，写操作要么加锁，要么移到 node-local 文件。

### 具体设计

当前顺序路径是：

```text
parent packet
  -> propose children
  -> for each hypothesis:
       create node
       run candidate
       maybe accepted_this_round.append(node)
  -> sort accepted
  -> update frontier
```

并行路径可以改成：

```text
parent packet
  -> propose children
  -> create nodes serially
  -> submit node runs to branch pool
  -> collect completed nodes
  -> merge insights / best score serially
  -> sort accepted
  -> update frontier
```

关键点是 `_run_candidate` 当前会直接调用 `branch_engine.run()`，而 `branch_engine.run()` 内部会调用 `merge_completed`。并行时应该拆成两段：

- worker thread 负责 sandbox、execute、evaluate、reflect，并返回 completed node。
- main thread 负责 `_merge_completed_node()`、budget record、event ordering、state persist。

如果短期不想重构 `BranchEngine`，可以先加一个 `merge_completed` lock，但这会让 state persist 在 worker 内发生，日志顺序也更难看。更干净的做法是让 `BranchEngine` 支持 `merge_on_complete=False`。

### Resource Gate

并行不能只等于 `min(children, max_parallel_branches)`。需要按资源类型收紧：

- GPU benchmark：默认 `max_parallel_branches=1`，除非 evaluator 明确支持多 GPU 或隔离 `CUDA_VISIBLE_DEVICES`。
- CPU-only / synthetic pipeline：可以并行到 CPU core 或配置上限。
- API-bound executor：可以并行，但要受剩余 agent 调用量、令牌预算、provider rate limit 限制。

建议把资源 gate 放在 search layer，而不是 task layer：

```text
planned branches
  -> annotate resource needs from task + planner policy
  -> scheduler grants branch slots
  -> ungranted branches stay queued or skip this round
```

这样保持 task 只描述需求，controller 决定执行。

### 和外部实现的关系

- Hermes 证明了 fresh-context child 可以并行，并且 tree-view monitoring 对调试很重要。
- Ray Tune 证明了 trial 并行必须受 resource request 控制，不能只看 trial 数量。
- LangGraph 提醒并行后更需要 checkpoint，避免完成的 branch 因主进程失败而丢失。

### 第一版验收标准

- `max_parallel_branches=1` 时行为和现有顺序执行一致。
- fake task 用 sleep 模拟 evaluator，`max_parallel_branches=2` 的总耗时明显低于顺序执行。
- 两个并行 branch 的 sandbox、stdout、stderr、evaluation logs 不交叉。
- frontier update 只发生在一轮所有 active branches 汇总之后。
- budget ledger 的 agent 使用量、API 调用量、令牌使用量、推理成本没有并发写坏。
- GPU task 默认仍然串行，除非显式开启并提供资源策略。

## 3. Role-Based Model Config

### 目标

把 planner / executor / reflector 从“不同 command”升级为显式 role。

当前 command 足够灵活，但表达不了这些稳定意图：

- planner 需要高推理能力，输出 Branch Card 和 Search Policy。
- executor 需要强 coding 能力，负责小范围修改。
- reflector 需要便宜稳定，压缩一个节点结果。
- verifier summary 尽量 deterministic，优先规则和结构化 evidence。

如果 role 不显式，后续成本分析只能看到 command，不能回答“哪个角色最贵、哪个角色收益最高、哪个角色应该换模型”。

### 推荐形态

新增轻量 dataclass：

```python
@dataclass(slots=True)
class AgentRoleConfig:
    command: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
```

`SearchConfig` 可以保留现有字段，同时加入：

```python
role_configs: dict[str, AgentRoleConfig] = field(default_factory=dict)
```

兼容规则：

- 如果 `planner_command` 存在，等价于 `role_configs["planner"].command`。
- 如果 `executor_command` 存在，等价于 `role_configs["executor"].command`。
- 如果 `reflector_command` 存在，等价于 `role_configs["reflector"].command`。
- role config 不要求 framework 直接调用 LLM API；它可以通过 env 注入给外部 command。

注入环境变量示例：

```text
TREE_AGENT_ROLE=planner
TREE_AGENT_MODEL=...
TREE_AGENT_PROVIDER=...
TREE_AGENT_REASONING_EFFORT=...
TREE_AGENT_TIMEOUT_SECONDS=...
```

这样不会打破 file-based contract。

### Role Policy

建议默认策略：

| 角色 | 默认能力取向 | 默认预算取向 | 输出 |
| --- | --- | --- | --- |
| planner | 强推理、低温度、结构化 JSON | 调用少，但单次可贵 | Branch Card、Bottleneck Card、Search Policy |
| executor | 强 coding、工具使用稳定 | 中等到高 | 修改 candidate，写 execution JSON |
| reflector | 便宜、稳定、短上下文 | 低 | 一条 insight |
| verifier summary | 规则优先，必要时便宜模型 | 很低 | keep / reject / need more evidence |

注意：planner 用强模型不代表让它写实现步骤。它的输出细节仍由 `planner_handoff_mode` 控制：

- `idea`: 只给高层 idea。
- `variant`: 给短 implementation variant。
- `detailed`: 给短 steps。

这和当前 `tree/planner.py` 的 handoff mode 一致。

### 和外部实现的关系

- Hermes delegation 支持给 subagent 配置独立 provider/model。
- AutoGen 每个 agent 可以有自己的 model client、system message 和 termination condition。
- 我们的角色更固定，因为 evaluator-driven tree 比自由对话更需要稳定 contract。

### 第一版验收标准

- CLI 可以配置 planner / executor / reflector 的独立 timeout。
- command logs 记录 role、model、provider、reasoning effort，但 secret 自动 redacted。
- budget summary 能按角色聚合：API 调用量、令牌使用量、推理成本、耗时、失败数。
- 旧 CLI flags 仍然可用。
- prompt 和 payload 不依赖具体模型名；模型选择只影响执行环境。

## 4. Timeout Diagnostics

### 目标

把“agent command timed out”变成可定位的问题。

当前 [tree/command.py](../../autopipeline/search/command.py) 已经有：

- `command_status.json`
- `stdout.log`
- `stderr.log`
- `cancel.request`
- timeout / cancelled / missing output / invalid output
- `activity.jsonl`
- `timeout_diagnostic.json`
- `env_redacted.json`

这些文件现在已经落地到每个 command logs dir。

`activity.jsonl` 记录 framework 观察到的活动：

```json
{"t": "...", "kind": "process_started", "pid": 123}
{"t": "...", "kind": "probe_request_changed", "bytes": 192}
{"t": "...", "kind": "probe_results_written", "bytes": 640}
{"t": "...", "kind": "stdout_changed", "bytes": 1024}
{"t": "...", "kind": "stderr_changed", "bytes": 512}
{"t": "...", "kind": "output_written", "bytes": 320}
```

`timeout_diagnostic.json` 写在 timeout / cancel / missing output / invalid output 时：

```json
{
  "action": "execute",
  "role": "executor",
  "status": "timed_out",
  "failure_class": "probe_wait_loop",
  "duration_seconds": 600.0,
  "timeout_seconds": 600.0,
  "started": true,
  "returncode": null,
  "command": "python3 agent.py execute",
  "output_exists": false,
  "last_activity_kind": "probe_request_changed",
  "last_activity_age_seconds": 511.2,
  "stdout_tail": "...",
  "stderr_tail": "...",
  "output_tail": "",
  "probe_request_tail": "...",
  "probe_result_tail": "..."
}
```

`env_redacted.json` 只记录非 secret 配置：

- role
- command argv or shell command summary
- cwd
- timeout seconds
- model/provider/base_url host
- file contract paths
- whether sensing was enabled

不要记录 API key、token、cookie。

### Zero-Call Definition

Hermes 的 zero-call timeout 指“子 agent 还没发生 API/tool call 就安静超时”。

我们这里可以定义为：

```text
process_started = true
and output_exists = false
and probe_result_count = 0
and no heartbeat/api usage event was recorded
and stdout/stderr size is 0 or only contains startup banner
```

为了让 external commands 上报更准确的 API activity，可以新增可选 heartbeat file：

```text
TREE_ACTIVITY_PATH=/.../activity.jsonl
```

外部 command 每次模型调用或工具调用前后追加：

```json
{"kind": "api_call_started", "model": "..."}
{"kind": "api_call_finished", "status": "completed", "tokens": 1234}
```

framework 不强制要求外部 command 实现 heartbeat；没有 heartbeat 时仍用 stdout/stderr/probe/output mtime 推断。

### Failure Classification

第一版只需要粗分类：

| 失败类别 | 判断依据 | 建议动作 |
| --- | --- | --- |
| provider unreachable | zero-call + stderr 有连接/auth/model 字样 | 检查 role config 和凭据 |
| command hung | 有启动，无输出，无 probe，无 stderr | 降低 timeout 或加 heartbeat |
| probe wait loop | probe request 写入后结果一直为空 | 检查 probe JSON 和 sensing runtime |
| missing output | returncode 0 但没写 output | 修外部 command contract |
| invalid output | output JSON 解析失败 | 修输出格式 |
| benchmark stall | executor 完成后 evaluator 超时 | 归到 task evaluate diagnostics |

### 第一版验收标准

- timeout 时一定生成 `timeout_diagnostic.json`。
- missing output / invalid output 也生成同类 diagnostic。
- diagnostic 至少包含 stdout/stderr tail、output 是否存在、probe request/result 状态、最后活动时间。
- secret redaction 有单测。
- budget ledger 能区分 timed out、missing output、invalid output。

## 6. Run-Local Motif Archive

### 目标

把“哪个方向有效”从自然语言 insight 变成 planner 可用的结构化 prior。

当前已有：

- `RootMemory.positive_insights`
- `RootMemory.negative_insights`
- `BudgetLedger.family_stats`
- branch `family`
- branch `idea_id`
- evaluator metrics

但这些还不够支持下一轮高质量 proposal：

- insight 太短，不一定包含 shape / hardware / bottleneck。
- family stats 只有粗粒度成败，不知道在哪类症状下有效。
- planner 不能稳定挑出“可复用 motif”和“应该冷却的 motif”。

### Motif 定义

motif 是一条经过 evaluator 观察的经验单元。

它不是“技巧大全”，而是：

```text
在某类 task / shape / hardware / bottleneck / branch family 下，
某个 idea 或 implementation variant 带来了什么结果。
```

建议写入 run-local 文件：

```text
run_dir/archive/motifs.jsonl
run_dir/archive/family_summary.json
run_dir/archive/planner_prior.json
```

### Motif Schema

```json
{
  "run_id": "matmul-tree",
  "node_id": "node-0007",
  "parent_id": "node-0003",
  "task_type": "autokernel",
  "scope": "kernel",
  "target_kind": "kernel_file",
  "kernel_or_problem": "matmul",
  "level": null,
  "family": "memory_hierarchy",
  "idea_id": "memory-access",
  "implementation_variant": "shared-memory-tiling",
  "symptoms": ["访存压力", "GPU 利用率不足"],
  "hardware": {"gpu_name": "H100", "compute_capability": "9.0"},
  "shape_signature": "B=..., M=..., N=..., K=...",
  "before": {
    "正确性": "PASS",
    "加速比": 1.12,
    "吞吐量": 42.0
  },
  "after": {
    "正确性": "PASS",
    "加速比": 1.31,
    "吞吐量": 49.0
  },
  "outcome": "accepted_improved",
  "score_delta": 0.19,
  "compile_status": "passed",
  "changed_files": ["kernel.py"],
  "reflection": "shared memory tiling improved coalescing",
  "cost": {
    "API 调用量": 2,
    "令牌使用量": 8300,
    "推理成本": 0.42,
    "执行耗时": 74.2
  }
}
```

用户-facing summary 用中文含义；内部 JSON 可以同时保留原 metric keys，但 planner prior 应优先给含义明确的字段。

### Outcome Vocabulary

建议 outcome 保持小集合：

- `accepted_improved`
- `accepted_flat`
- `accepted_regressed`
- `rejected_correctness`
- `rejected_compile`
- `rejected_runtime`
- `timeout`
- `budget_pruned`

这些结果比简单 positive / negative 更适合 planner：

- `rejected_compile` 应该触发 family cooldown 或更保守 executor handoff。
- `accepted_flat` 可以作为“方向可行但收益不足”的证据。
- `timeout` 不一定说明 motif 错，可能说明 evaluator 或 command 配置有问题。

### Planner Consumption

planner 不应该直接读所有 motifs。应该读一个压缩后的 `planner_prior.json`：

```json
{
  "top_positive_motifs": [
    {
      "family": "memory_hierarchy",
      "idea_id": "memory-access",
      "condition": "访存压力 + contiguous shape",
      "expected_signal": "提高加速比",
      "confidence": 0.7
    }
  ],
  "cooldown_families": [
    {
      "family": "instruction_selection",
      "reason": "最近 3 次没有通过编译"
    }
  ],
  "avoid_repeats": [
    "same idea_id + same implementation_variant already explored on current target"
  ]
}
```

这个 prior 应进入 `task.planner_context()` 或 `Planner._with_budget_context()` 的 history context。

planner 的输出仍然是 Branch Card：

- motif 只影响 branch family 和 rationale。
- motif 不直接替 executor 写代码。
- motif 的 confidence 只能调节 exploration bias 和 branch budget，不应绕过 evaluator。

### 和 DSPy 的关系

DSPy 的 optimizer 把 traces、metric、proposal search 串起来。我们的 motif archive 可以看成面向 kernel / pipeline optimization 的低配版 optimizer memory：

- trace = branch logs + benchmark logs + reflection。
- metric = 正确性、加速比、基准测试耗时、推理成本。
- proposal = Branch Card。
- search = TreeSearchEngine frontier。

区别是 DSPy 优化 prompt / program；我们优化 candidate code 或 pipeline workspace。

### 第一版验收标准

- 每个 completed / failed / pruned branch 产生一条 motif 或 rejection record。
- archive 不写入全局 memory，不修改 repo rules。
- planner payload 能看到压缩后的 positive motifs、cooldown families、avoid repeats。
- family summary 能回答：每个 family 的尝试次数、采纳次数、平均分数变化、编译失败次数、平均推理成本。
- 同一 run resume 时不会重复写同一个 node 的 motif。

## Supporting Item: Run Viewer

run viewer 不是第一批必须实现的核心逻辑，但会显著提高调参效率。

最小可用版本可以是一个 CLI：

```bash
uv run scripts/tree_report.py runs/<run-name>
```

输出中文含义表：

| 节点 | 父节点 | 状态 | 分支家族 | 正确性 | 加速比 | 基准测试耗时 | 推理成本 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

后续再做 `/agents` 类 TUI：

- 按 parent / branch 展示树。
- 每个 branch 展示 executor iteration。
- 展示令牌使用量、API 调用量、推理成本、触达文件。
- 支持查看 stdout/stderr/status/timeout diagnostic。

这个 viewer 体现的 story 是：branch 是实验 trial，不是聊天消息。

## Supporting Item: Probe Availability

当前 sensing manifest 已经列出 built-in probe handlers 和 task probe handler 是否存在。

下一步应该从“列名字”升级成 availability registry：

```json
{
  "available_probes": [
    {"kind": "read_text", "enabled": true},
    {"kind": "ncu_profile", "enabled": false, "reason": "ncu not found"},
    {"kind": "gpu_status", "enabled": false, "reason": "no visible NVIDIA GPU"}
  ],
  "probe_budget": {
    "remaining": 2
  }
}
```

这样 planner / executor 不会反复请求不可用 probe。

这个机制借鉴 Hermes tool registry 的 `check_fn` 思路，但落点不同：Hermes 是隐藏不可用 tools；我们是把诊断能力和不可用原因写给 planner。

## Supporting Item: Pipeline Trajectory

Hermes environments 的 `item -> rollout -> reward -> trajectory` 对 Level 5 有启发。

我们可以把 pipeline task 也表达成：

```text
item: 一个 composed workload / pipeline region
rollout: planner -> branches -> executor local loops
reward: end-to-end latency / throughput / cost / correctness
trajectory: branch tree + metrics + motifs
```

这能把 Level 5 从“多个 kernel 拼起来”升级成“有 region、trace、reward、trajectory 的实验对象”。

但当前不要把 AutoKernel single-kernel task 伪装成 full pipeline optimizer。
`pipeline/bridge.py` 和 `pipeline/task.py` 已经提供 spec-driven generic
pipeline workspace path；下一步仍然是补真实 profiler 和 trace schema。
正确顺序是：

1. 先让现有 `PipelineTask` 接入真实 profiler 和 trace schema。
2. 再让 planner 消费 pipeline bottleneck。
3. 再释放 fusion、layout propagation、launch consolidation 这类跨 kernel family。

## Recommended Implementation Order

建议顺序不是按重要性，而是按依赖关系：

1. 超时诊断
   - 并行之前先把失败看清楚。
   - 修改面集中在 `tree/command.py` 和少量 tests。

2. role-based model config
   - 让 planner / executor / reflector 的成本和失败可区分。
   - 修改 `SearchConfig`、CLI、`CommandAgentAdapter`、`Planner` env 注入。

3. run-local motif archive
   - 先从现有顺序执行收集 motif。
   - 不依赖并行，风险低。

4. parallel branch execution
   - 在 diagnostics、role budget、archive 都能记录结果后再并行。
   - 第一版只支持 local thread pool 和 conservative resource policy。

5. run viewer
   - 先做 CLI report，再考虑 TUI。

6. probe availability registry
   - 可以和 timeout diagnostics 或 sensing cleanup 一起做。

7. pipeline trajectory
   - 等 pipeline profiler / trace schema 明确后再做。

## Acceptance Matrix

| 改进项 | 可见结果 | 验证方式 |
| --- | --- | --- |
| 并行 branch execution | 同一轮多个 branch 同时运行，输出仍隔离 | fake sleep task 比顺序更快；检查 node logs 不交叉 |
| role-based model config | 每个角色有独立 command/model/timeout/cost 聚合 | 单测 role env；检查 budget summary |
| 超时诊断 | timeout / missing output / invalid output 都有诊断 JSON | 构造 timeout command；检查 stdout/stderr tail 和 failure class |
| run-local motif archive | 每个节点有可复盘 motif/rejection record | 跑 fake tree；检查 archive JSONL 和 planner prior |
| run viewer | 人能快速看懂一轮树的状态与成本 | 对 fixture run 输出中文表 |
| probe availability | agent 知道哪些 probe 可用、不可用原因是什么 | sensing manifest 单测 |
| pipeline trajectory | pipeline run 有 item、reward、trajectory | pipeline smoke fixture 输出 trajectory |

## Design Guardrails

- 不把 Hermes 的 `todo` 等同于我们的 planner。
- 不让 motif archive 变成全局长期 memory。
- 不让 parallelism 绕过 evaluator。
- 不把 benchmark raw key 直接作为用户-facing summary。
- 不为了并行牺牲 sandbox isolation。
- 不把 AutoKernel single-kernel task 描述成已经支持完整 Level 5 pipeline optimization。

## Short Conclusion

最值得吸收的不是某个框架的表面 API，而是四个可组合的工程原则：

1. 像 Hermes 一样让执行单元 fresh-context、受限、可监控。
2. 像 Ray Tune 一样把 branch 当 trial，用资源和评价函数管理并行。
3. 像 LangGraph 一样把长流程做成可恢复、可复盘的 step。
4. 像 DSPy 一样把成功和失败 trajectory 压缩成下一轮 proposal prior。

落到本仓库，就是：

```text
profile / sensing / history
  -> planner compresses priors into Branch Cards
  -> branch pool runs isolated trials
  -> evaluator and verifier decide keep/reject
  -> motif archive feeds the next planner call
```

这条链路正好服务本项目的核心目标：用更低 API 成本、更快迭代速度，获得更高的实测实现质量。
