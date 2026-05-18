# Symphony Agent Runtime 对比报告

更新日期：2026-05-14

这份报告只回答一个问题：

> 怎样把当前库从“能跑实验的工具”推进到“可以让 coding agent 稳定开发、持续运行、可观察、可恢复的工具”？

参考对象是本地的 Symphony 仓库。重点不是照搬它的 Linear 工作流，而是学习它怎样启动、管理、通讯、调用和同步 agent。

## 一句话结论

当前库已经不是简单 toy。它有 sandbox、tree search、branch-local loop、evaluator、sensing、超时诊断、预算记录和 run viewer。这些是研究型 agentic optimization 工具的核心骨架。

但它还没有达到“真正开发使用工具”的完整形态。主要差距是：当前系统把 agent 当成一次命令执行，而 Symphony 把 agent 当成一个可管理会话。会话有启动、turn、事件、工具调用、token、失败、续跑、状态快照和 operator 可见状态。

因此不建议重新设计一个新的调度器。更好的路线是在现有 tree runtime 上补一层 agent session / event / state sync 能力，让现有 command bridge 继续可用，同时让 Codex 这类 agent 从“一次性黑盒命令”变成“运行中可观察、可控制、可继续的开发者工具”。

## 对比口径

Symphony 的主线是：

1. 从外部工作源拿到一个任务。
2. 为这个任务创建独立 workspace。
3. 在 workspace 里启动 Codex app-server。
4. 开一个 thread，再开一个 turn。
5. 持续接收 agent 事件。
6. 把事件合并进 orchestrator 状态。
7. 根据任务状态、失败、超时、stall 来停止、重试或续跑。
8. 把运行状态展示给 operator。

本项目的主线是：

1. 为优化任务创建 baseline 和 branch。
2. 为每个 branch 创建 sandbox。
3. 把 prompt、payload、telemetry 写给外部 agent command。
4. 启动子进程，期间服务 probe 请求。
5. 等子进程写回 JSON。
6. evaluator 决定 keep / revert。
7. 搜索状态、预算、viewer 记录最终结果。

两边真正相同的是“任务 -> workspace/sandbox -> agent -> 结果 -> 状态更新”。不同的是，Symphony 的 agent 运行过程是一等对象；本项目的 agent 运行过程主要还是命令执行的副产物。

## 当前库已经具备的基础

本项目已经有几个很重要的工程基础，不能被低估：

- 每个候选分支都有独立 sandbox，适合做并行 trial。
- evaluator 是裁决面，agent 自述不会直接决定结果。
- branch-local loop 已经有 keep / revert，这比普通聊天 agent 更接近实验系统。
- command bridge 已经有超时、取消、失败分类和诊断文件。
- sensing 已经能在 agent 运行时提供只读信息和受控 probe。
- budget ledger 已经能记录 agent 调用、probe 使用、成本和失败。
- run viewer 已经能把一次实验整理成可复盘输出。

所以当前库的短板不在“有没有调度逻辑”。短板在 agent runtime 的产品化程度。

## 核心差距

### 1. Agent 生命周期还不是一等对象

Symphony 明确区分：

- agent 进程是否启动成功；
- thread 是否创建成功；
- 当前 turn 是否开始；
- turn 是否正常结束；
- 是否需要人工输入；
- 是否发生工具调用；
- 是否进入失败、取消、超时或 stall。

本项目现在也会记录进程开始、超时、取消、输出文件变化等信息，但这些更像进程层面的观察，不是 agent 语义层面的生命周期。

这会带来一个直接问题：当一个 executor 卡住时，我们知道“文件多久没变”，但不一定知道它是卡在模型请求、工具审批、probe 等待、命令执行，还是 agent 已经完成但 wrapper 没写结果。

### 2. 通讯协议偏文件交换，不是事件流

本项目的文件协议是合理的，也适合保留。prompt、payload、telemetry、output、probe request/result 都很清晰。

差距在于：文件协议现在主要服务“输入和最终输出”，对“运行中发生了什么”表达得不够细。Symphony 从 app-server 收 JSON-RPC 消息，再把这些消息转成运行事件，orchestrator 可以实时更新状态。

本项目如果继续只看 stdout/stderr 和文件大小，后续很难做稳定的 live status、stall 判断、token 成本跟踪和精确失败恢复。

### 3. 状态同步还停在 run / node 层

本项目有 `search_state`，这对实验复盘很有价值。但运行中的 agent 状态没有被稳定汇总到 search state。

现在一次 agent step 的状态主要散落在 step log 目录里。tree engine 最后知道这个 node 成功或失败，但 operator 很难在运行中回答：

- 这个 agent 现在处于哪一步；
- 最近一次有意义活动是什么；
- 这次 turn 已经消耗多少 token；
- 当前失败是否适合重试；
- 是否能从已有 workspace 继续。

Symphony 的做法是把 agent update 合并进 orchestrator runtime state，然后 snapshot / dashboard 直接读这个状态。

### 4. Codex 集成边界偏薄

当前 Codex 仍保留 `codex exec` wrapper 作为兼容路径。同时，planner、executor、reflector 已经可以通过统一 role backend 走 Codex app-server session。该路径可以创建或恢复 thread、启动 turn、接收事件，并记录 token usage、diff、dynamic tool 调用和 activity。

Symphony 使用 app-server 模式，能明确创建 thread、启动 turn、处理工具调用、自动审批、捕获 token usage，并且可以在同一个 session 里继续多个 turn。

因此当前差距已经不是“有没有 app-server adapter”，而是：

- 是否继续把 Codex app-server 保持为 Codex role 的默认 backend；
- 是否把更多 sensing probe 以 dynamic tool 形式暴露，而不是只依赖文件轮询；
- 是否让同一个 branch / role 复用 thread 来支持 branch-local continuation；
- 是否把 session / turn 状态合并进 run state 和 viewer。

### 5. 工具调用还没有形成统一 capability 面

本项目的 sensing / probe 很适合 optimization 场景，比 Symphony 的 Linear tool 更贴近本项目目标。

但当前 agent 看到的是若干路径和说明，不是一个清晰的“可用工具清单 + 参数约束 + 失败原因 + 使用预算”。Symphony 的动态工具虽然很少，但它作为 app-server 的工具能力被显式注册，调用、失败和结果都能进入事件流。

本项目可以借鉴这个形式，但内容应该是自己的：probe、benchmark、profile、roofline 输入、candidate diff、workspace inspection 这些才是核心能力。

### 6. Operator 视角还偏事后查看

viewer 对 run 后复盘已经有价值，但真正的开发工具还需要运行中状态。

Symphony 的状态面不一定高级，但它能回答当前有多少 agent、每个 agent 在哪个任务、跑了多久、最后事件是什么、消耗多少 token、哪些任务在 backoff。

本项目可以先做很小的 CLI watch 或 summary，不需要马上做 web UI。关键是把 agent step 的最新状态纳入统一状态面，而不是要求人去翻每个 log 目录。

## Toy Tool 到开发工具的分界线

一个工具从 toy 变成开发可用，不是因为 UI 更复杂，而是因为它满足这些条件：

1. 每次 agent run 都有明确生命周期。
2. workspace 边界稳定，agent 不能靠隐式上下文工作。
3. 运行中有语义事件，而不是只有 stdout 和最终结果。
4. 状态可以被查询，可以解释现在在做什么。
5. 失败能被分类，并能驱动 retry、abort 或继续。
6. 续跑不是“重新开始”，而是带着已有 workspace 和明确上下文继续。
7. operator 不需要读源码就能判断当前系统是否健康。
8. 测试覆盖的是 runtime contract，而不是只覆盖 happy path。

按这个标准，本项目现在是一个有强实验骨架的研究工具，但还不是完整的开发者运行平台。

## 最值得借鉴的五点

### 1. 把 agent run 变成 session record

建议新增一个轻量概念：每次 agent step 都有一个运行记录。

它不需要一开始就是数据库，也不需要新调度器。可以先由现有 log 目录和 search state 共同承载。

这个运行记录应该回答：

- 它属于哪个 node / branch / iteration；
- 它运行在哪个 workspace；
- 它启动了哪个 agent；
- 当前处于什么状态；
- 最近一次有意义活动是什么；
- 最终结果在哪里；
- 失败时为什么失败。

这样后续 viewer、watch、retry、stall 诊断都可以围绕同一份事实工作。

### 2. 把 activity 从“文件变化”升级成“语义事件”

现有 activity 文件可以保留，但事件内容要更有意义。

短期可以先由 wrapper 写这些事件：

- agent 已启动；
- prompt 已发送；
- agent 子进程已启动；
- agent 有输出；
- agent 写入结果；
- agent 正常结束；
- agent 异常结束；
- wrapper 判断 workspace 有变更。

如果将来接 app-server，再补充：

- session 已创建；
- turn 已开始；
- 工具调用开始 / 结束；
- token usage 更新；
- turn 完成 / 失败 / 取消；
- agent 请求人工输入。

重点不是字段名字，而是让系统能理解“agent 正在做什么”。

### 3. 把 agent update 合并进统一状态

每个 agent step 的最新状态应该汇总到 node 或 run state，而不是只留在 step log 目录。

这样运行中可以直接看到：

- 哪个 branch 正在执行；
- 最近一次活动是什么；
- 是否可能 stall；
- 当前消耗；
- 如果失败，失败类型是什么；
- 这个失败是否影响整个 run，还是只剪掉当前 branch。

这一步能直接提升“开发工具感”，因为用户不需要翻目录才能理解系统。

### 4. 控制式续跑，而不是无边界长会话

Symphony 的续跑值得学，但不要无脑把所有 executor 都改成长会话。

本项目更适合 branch-local continuation：

- 同一个 branch / sandbox 内可以复用 session；
- 每次 continuation 都带上 evaluator 反馈；
- 有最大 turn 数；
- 每个 turn 结束后仍然由 evaluator 裁决；
- 一旦方向被证明无效，就退出或回退。

这样既能利用 agent 的上下文连续性，又不会让 agent 脱离 evaluator-driven loop。

### 5. 工具能力要注册、可见、可测

当前 sensing 的方向是对的，但需要更像产品接口：

- agent 能看到本次有哪些 probe 可用；
- 每个 probe 的输入约束清楚；
- 超预算、不可用、执行失败都有稳定结果；
- 工具调用进入事件流；
- viewer 能展示工具调用对最终判断的贡献。

这会让 agent 不再靠“猜命令”工作，而是使用 framework 暴露的受控能力。

## 不建议照搬的地方

不要照搬 Symphony 的 issue tracker daemon。当前项目的核心不是自动扫票，而是 evaluator-driven optimization。

不要把现有 `TreeSearchEngine` 推翻重做成新的调度器。当前调度骨架能支撑 branch、frontier、budget、archive 和 evaluator，应该增强它周围的 agent runtime，而不是换名字重写。

不要让 agent 自己决定实验是否成功。Symphony 的 coding agent 面向开发任务，可以通过 ticket / PR / review 闭环判断进度。本项目必须继续让 evaluator 和 verifier 做裁决。

不要一开始就引入持久数据库。先把事件和状态写清楚，再决定是否需要真正 durable queue。

## 建议的实现顺序

### P0：语义化 agent activity

目标：不改变现有 command bridge 的调用方式，只提升运行中可见性。

建议动作：

- 让 wrapper 在关键节点写 agent 事件。
- command bridge 继续记录进程事件和文件变化。
- timeout 诊断优先使用最近的语义事件，而不是只看文件大小。
- viewer 或 summary 能展示每次 attempt 的最后活动和失败原因。

验收标准：

- 现有 command contract 不变。
- 没有配置 Codex 时仍然能跑现有测试。
- fake agent 可以模拟正常结束、无输出、超时、stderr 报错。
- 每种情况都有可读的最后活动和失败原因。

### P1：把 agent step 状态并入 run state

目标：运行中能直接知道每个 branch 的 agent 在做什么。

建议动作：

- node 记录当前 step 的状态摘要。
- 每次 command 状态变化后，tree state 能看到最新摘要。
- run viewer 展示 attempt 的 agent 状态、最近活动、失败类型、耗时和成本。

验收标准：

- 人不打开 step log 目录，也能知道失败发生在哪个 branch / iteration。
- branch 并行时不会互相覆盖状态。
- 状态摘要里不暴露敏感环境变量或完整 secret。

### P1：Codex app-server 默认化和 session 状态

目标：在已完成 role-level app-server backend 的基础上，把 session runtime 纳入更统一的 run state / viewer。

建议动作：

- 保留 `codex exec` 作为 legacy fallback。
- Codex planner / executor / reflector 默认走 app-server backend。
- 把 session / turn / usage / completion 事件合并进统一 activity 和 run state。
- dynamic tool 先不要做大；优先把已有 sensing probe 暴露成受控工具。

验收标准：

- `codex exec` 路径继续可用。
- app-server 路径继续覆盖 fake app-server 测试。
- session / turn / tool / usage / completion 事件能进入同一 activity。
- turn 失败、需要人工输入、超时都能被明确分类。

### P1：branch-local continuation

目标：让同一个 branch 可以带着 evaluator 反馈继续，而不是每次都从冷启动 prompt 开始。

建议动作：

- continuation 只在同一个 branch 和 sandbox 内发生。
- 每次 continuation 都带上上一次 evaluator 结果和明确剩余目标。
- 最大 turn 数和预算由 framework 强制执行。
- evaluator 仍然决定 keep / revert。

验收标准：

- continuation 不改变现有单步执行语义。
- 一个 branch 的连续 turn 共享上下文，但不会污染其他 branch。
- 如果 evaluator 连续拒绝，branch 能停止而不是无限聊天。

### P2：实时状态面

目标：先做轻量 CLI/watch，不急着做复杂 UI。

建议动作：

- 从 search state 和 agent activity 生成 live summary。
- 展示 running、recent activity、failed、best-so-far、budget。
- 对 stall 给出原因倾向，而不是只显示 timeout。

验收标准：

- 长 run 过程中，用户可以不翻日志就判断系统是否在工作。
- 并行 branch 下能看到每个 branch 的状态。
- 结束后 live summary 和 viewer 的结论一致。

### P2：恢复和重放边界

目标：把已完成步骤和半完成步骤区分清楚，为后续 resume 做准备。

建议动作：

- 明确哪些 step 完成后可复用，哪些必须重跑。
- 将 agent command、evaluation、promotion 都视为可记录的步骤。
- 遇到中断时，先只做“识别并报告可恢复点”，不要急着自动恢复。

验收标准：

- 中断后能列出已完成 node、运行中断的 node、缺失输出的 step。
- 不会重复 promotion 或重复采纳未验证的候选。
- 人能根据报告决定从哪里继续。

## 给 coding agent 的实现边界

第一轮实现不要碰搜索策略，不要重新设计 scheduler，也不要改变 evaluator 裁决。

推荐第一轮只做：

1. 语义化 activity。
2. agent step 状态摘要。
3. viewer / summary 展示最后活动和失败原因。
4. 针对 wrapper、timeout、invalid output、probe wait 的测试。

第二轮再做：

1. Codex app-server run state / viewer 展示。
2. session / turn event 统一到同一 activity。
3. branch-local continuation。

第三轮再做：

1. live status。
2. resume report。
3. 更完整的 capability registry。

## 最小可交付切片

如果只做一个小切片，我建议做：

> 让每次 executor run 产生可读的语义事件，并把最后一个语义事件展示到 run viewer。

这个切片小，但价值高。它不会改变核心算法，也不会引入新调度器，却能立刻改善三个问题：

- agent 卡住时更容易判断原因；
- run 后更容易复盘每个 attempt；
- 后续接 app-server 时有统一事件落点。

这也是从 toy tool 走向开发工具的第一步：系统不再只知道“命令结束了吗”，而是知道“agent 运行时发生了什么”。
