# Watchman v1

这份文档记录 `watchman` 在 tree 适配层中的 v1 目标。
它不讨论低层实现细节，而是说明：

- 要解决什么痛点
- 提出了什么架构
- 预期效果是什么
- 哪些方向已经进入 v1
- 哪些方向留给后续实验矩阵

## 为什么要有 Watchman

当前 tree adapter 已经能做搜索控制：

- 分支展开
- 前沿保留
- 剪枝
- 节点隔离
- evaluator / verifier 驱动的裁决

但这并不自动意味着搜索有效。

目前最核心的问题不是“树不够深”，而是：

- proposal 先验太弱
- 分支语义不够正交
- 高层决策没有被冻结
- pipeline bottleneck / hardware / history 没有系统性进入 proposal

如果这些问题不解决，增加 beam width 和 round 数只会扩大低质量探索。

## Watchman 要解决的痛点

### 1. Proposal 先验太弱

系统需要在 proposal 之前就拥有更有根据的高层判断，而不是从通用启发式重新开始。

### 2. 分支容易伪多样化

不同分支看起来不同，但实际常常还是同一种优化机制的近似变体。

### 3. 高层决策不够稳定

没有被冻结的高层分支身份，会让 executor 在节点内部重新“发明方向”，导致树的语义漂移。

### 4. 历史经验没有被压成 proposal 可用的结构

成功与失败如果只停留在日志里，就不会真正改善下一轮探索。

### 5. Coding Agent 会持续变强，但系统结构未必能吸收这种进步

我们的判断是：

- coding agent 的局部实现能力会持续提升
- 因此系统最有价值的工作，不是和 agent 抢执行，而是做更好的引导
- `watchman` 的定位应是能力放大器，而不是能力替代者

## Watchman v1 的架构

v1 的 `watchman` 不负责写代码，也不负责裁决节点分数。
它只负责高层决策冻结。

具体说，它做四件事：

### 1. 融合四类先验

v1 直接融合这四类输入：

- 静态结构先验
  例如 kernel 类型、shape、rank、profile 导出的描述
- 硬件先验
  例如 GPU 型号、compute capability、可见设备上下文
- 当前局部 evaluator 先验
  在今天的单-kernel harness 里，这部分来自 `bench.py` 的摘要
- 历史先验
  例如最近的正向 insight、负向 insight、已探索假设

这里要明确一件事：

- `bench.py` 只是当前 AutoKernel 单-kernel 适配器里的局部 evaluator
- 它不是未来通用 framework 的核心 prior 来源
- 如果目标扩展到任意 pipeline，真正重要的是 pipeline bottleneck profile 和硬件 profile

### 2. 产出 Branch Card

`watchman` 的输出不是“几个随意的想法”，而是带有高层身份的 Branch Card。

每张卡至少回答：

- 这条分支属于哪类机制家族
- 它依据了哪些 profile / pipeline bottleneck / hardware / history 信号
- 它为什么和别的分支不同
- 它预期改善什么信号
- executor 不应该偏到哪里去

### 3. 保持机制层的分支正交

v1 不按文本多样性做 proposal，而是按 mechanism family 做 proposal。

也就是说，branch diversity 的目标不再是：

- “多写几种不同表述”

而是：

- memory hierarchy
- parallelization
- occupancy / launch
- latency hiding
- instruction selection
- control-flow specialization

这些机制层差异，才是 kernel tree search 真正要保留的多样性。

### 4. 把高层决策传递给执行层

`watchman` 产出的 Branch Card 会进入 proposal prompt，也会进入 execution prompt。

这意味着：

- proposal 被高层先验约束
- executor 也会看到自己所处的机制家族与 guardrail
- 高层方向不会轻易在节点执行阶段漂移

如果外部 planner 只返回旧版的最小 hypothesis 字段，adapter 也会按 watchman 的分支卡回填 family 和 guardrail，避免高层身份在 planner -> executor 这条链路里丢失。

## Watchman v1 的预期效果

如果这套压缩和控制有效，预期会产生正提升，原因不是树更复杂，而是树更有根据。

预期收益主要体现在：

### 1. 更少的低信息探索

proposal 不再从弱启发式重新开始，而是带着 profile / hardware / history 的先验进入每一轮。

### 2. 更强的分支正交性

不同分支更可能代表不同优化机制，而不是同一路线的轻微变体。

### 3. 更稳定的高层决策

executor 的角色更清楚，分支身份更清楚，credit assignment 也会更清楚。

### 4. 更容易吸收未来更强的 coding agent

随着 executor 变强，系统不必推翻整体结构。
`watchman` 继续提供高层先验，tree 继续管理搜索，executor 只需要在节点内部变得更强。

这正是“能力放大器”的含义。

## 为什么这是一种可持续的设计

我们当前最重要的判断是：

- coding agent 的能力还会持续提升
- 因此最值得投入的，不是写死某一种局部优化技巧
- 而是做那些在 agent 持续变强时仍然有效的外部结构

`watchman` 属于这一类结构。

它的价值在于：

- 对弱一点的 agent，它提供高层引导
- 对强一点的 agent，它提供更好的搜索先验
- 对更强的 agent，它依然可以作为 hierarchy 的高层接口

所以 `watchman` 不是短期 patch，而是一个可以持续吸收模型进步的架构点。

## v1 已经纳入的方向

v1 已经明确纳入：

- profile 信息进入 proposal 前的高层上下文
- hardware context 进入 proposal prior
- history / insight 进入 proposal prior
- mechanism family 驱动的 branch identity
- Branch Card 进入 executor prompt 作为 guardrail
- 当前 AutoKernel adapter 能把这些先验接到单-kernel tree harness 上
- 当前代码已经能承载轻量的 `RegionGraph` / `BottleneckCard`，也能读入 pipeline 风格的 scope、region、upstream/downstream、bottleneck hints
- 但在今天的 AutoKernel 单-kernel harness 里，`watchman` 仍然只会产出单文件可执行的 kernel-local family，不会假装当前 adapter 已经能执行 fusion、layout propagation、launch consolidation 这类跨 kernel 修改

## v1 还没有解决的关键抽象

v1 目前主要服务于单-kernel 优化。

这足够验证 tree adapter 和 `watchman` 的基本形态，但还不够支持更一般的 pipeline 优化。

这里当前代码和长期目标要明确分开：

- 现在已经有的是“pipeline 风格诊断输入接口”
- 现在还没有的是“真正的 pipeline task / profiler / editor”

所以当前 `watchman` 可以先利用更宽的 pipeline 上下文帮助单-kernel proposal 不那么盲，但它不会在 AutoKernelTask 里直接释放 pipeline-level treatment family。

下一步最重要的不是把树做得更复杂，而是补齐下面这层抽象：

- pipeline 的 bottleneck 到底应该怎样表示
- 多 kernel 的相互作用怎样进入 proposal
- fusion、layout 传播、跨 kernel 调度、host-device overlap 这类问题怎样形成 branch family
- 哪些观测属于“理解 pipeline”，哪些观测已经是“修改 pipeline”

换句话说，后面的重点会从：

- “给单个 kernel 提更好的方向”

扩展成：

- “给一个 pipeline bottleneck 区域提更好的方向”

## 下一步的工作方向

### 1. 抽象 pipeline bottleneck

不是只看单个 kernel 快不快，而是要描述：

- 哪个阶段最慢
- 慢是因为算力、访存、同步、调度、launch、通信，还是 kernel 之间衔接不好
- 这个瓶颈是单 kernel 问题，还是多 kernel / region 问题

这里最重要的不是“起一个名字”，而是把诊断和治疗分开。

如果用更直白的话说，后面的 framework 应该像：

- 先看症状
- 再判断病因
- 最后给治疗方案

也就是：

- `signals`: 先收集 pipeline profile、hardware profile、trace、阶段耗时、利用率、等待时间、kernel 边界开销
- `diagnosis`: 再判断这是 compute、memory、sync、launch、communication、layout、fusion 机会，还是多种问题叠加
- `treatment`: 最后再决定给哪类优化方案

后面的 `watchman` 不应该直接从“看到了一个指标”跳到“随便给几个方向”。
它应该先输出一个稳定的 bottleneck diagnosis，再由 diagnosis 派生 branch family。

### Pipeline Bottleneck Schema

后面建议把一个 pipeline bottleneck 表示成三层：

#### 1. Observation

只记录事实，不下结论。

例如：

- 哪个 stage 最慢
- 每个 stage 占总时间多少
- GPU 利用率高还是低
- 带宽利用高还是低
- 是否存在明显 launch 开销
- kernel 之间是否有明显空洞
- 是否存在大量小 kernel
- 是否有频繁 layout 转换或中间写回
- 是否存在通信等待或 host-device 等待

#### 2. Diagnosis

把 observation 压成更稳定的问题类别。

建议至少覆盖这些大类：

- compute saturation
- memory bandwidth pressure
- latency / launch overhead
- synchronization stall
- communication stall
- layout churn
- cross-kernel boundary overhead
- fusion opportunity
- pipeline overlap failure
- mixed bottleneck

这里要允许“联合诊断”。
真实 pipeline 往往不是单一问题，而是：

- memory + launch
- compute + layout
- communication + overlap
- fusion opportunity + synchronization

也就是说，一个 bottleneck 区域不一定只有一个标签。

#### 3. Treatment Family

诊断出来以后，再映射到优化家族。

例如：

- compute saturation -> instruction selection / tensorization / tile tuning
- memory bandwidth pressure -> memory hierarchy / layout propagation / staging
- latency / launch overhead -> launch consolidation / kernel batching / fusion
- synchronization stall -> async pipeline / overlap / stage restructuring
- communication stall -> communication-compute overlap / bucketization / schedule changes
- layout churn -> layout propagation / intermediate elimination / region fusion
- cross-kernel boundary overhead -> fusion / producer-consumer co-design / launch consolidation
- pipeline overlap failure -> overlap / pipelining / host-device schedule changes

这一步的重点是：

- diagnosis 负责“问题是什么”
- treatment family 负责“值得优先试什么类方案”

### 联合问题的联合方案

你刚才强调得对，真实系统里很重要的一点不是只给单个问题单个答案，而是给多个问题的联合方案。

所以后面不能只做：

- 一个 bottleneck -> 一个 branch

还要支持：

- 一个 bottleneck region -> 一个主诊断 + 一个次诊断
- 一个 branch card -> 一个主方案 + 一个允许联动的副方案

例如：

- 主诊断是 cross-kernel boundary overhead，次诊断是 launch overhead
- 那么主方案可以是 fusion，副方案可以是 launch consolidation

或者：

- 主诊断是 memory bandwidth pressure，次诊断是 layout churn
- 那么主方案可以是 layout propagation，副方案可以是 memory hierarchy rewrite

这样 tree search 就不会只会“单点治疗”，而能开始处理 region 级联合优化。

### 2. 明确 hardware profile 的角色

对通用 framework 来说，硬件 profile 不是辅助信息，而是 proposal 的核心输入之一。

它应该帮助回答：

- 当前设备更容易被什么限制住
- 哪类方向在这块卡上更值得优先试
- 哪些方向虽然在 benchmark 上看起来有收益，但在真实 pipeline 上不稳定

### 3. 扩展 branch family

今天的 family 主要偏向单 kernel：

- memory hierarchy
- parallelization
- occupancy / launch
- latency hiding
- instruction selection
- control-flow specialization

后面要增加更偏 pipeline 的 family，例如：

- kernel fusion
- layout propagation
- stage reordering
- overlap / pipelining
- launch consolidation
- communication / compute overlap

这些 family 不能只是一个列表，后面应该分成两层：

- primary family: 这条分支主要治疗什么问题
- secondary family: 当出现联合 bottleneck 时，允许的联动方向是什么

这样 `watchman` 才能在保持 branch identity 稳定的同时，支持“联合方案”。

### 4. 把 evaluator 和 prior 分开理解

对单-kernel AutoKernel 适配器，`bench.py` 很适合做 evaluator。

但对通用 pipeline framework：

- evaluator 可以是端到端 latency、阶段 latency、trace、profile report
- prior 不应该简单依赖 benchmark 指标
- prior 更应该来自 pipeline 分析、硬件 profile、历史成功模式

## 后面 `watchman` 应该怎样用这个 schema

如果把这套 schema 接到 `watchman`，理想流程应该是：

1. 先读 profile / trace / hardware context / history
2. 生成一个 bottleneck observation
3. 把 observation 压成 diagnosis
4. 根据 diagnosis 选择 primary / secondary treatment family
5. 再生成 branch card

也就是说，后面的 branch card 不应该只是：

- “试试 memory”
- “试试 tile”

而应该更像：

- 这个 region 的主问题是 cross-kernel boundary overhead
- 次问题是 launch overhead
- 这轮主方案是 fusion
- 联动副方案允许 launch consolidation
- 不要偏航到 instruction-selection 这类不对应当前病因的方向

## 先不放进 v1 的方向

下面这些方向很重要，但先作为后续实验矩阵的一部分，而不是 v1 必做：

- 复杂 compute allocation
- learned cost model / surrogate
- archive 的 motif 组合重组
- 语义级去重
- 复杂的不确定性感知剪枝
- pipeline bottleneck 的更细粒度观测

这些方向值得做，但放在 v1 会模糊掉最关键的验证问题：

> 单靠更强的高层先验和更稳定的分支身份，能否带来可见的搜索质量提升？

## 后续实验矩阵的建议

后续矩阵更适合围绕以下几条轴展开：

### 1. Prior 来源

- 只用静态结构
- 静态结构 + pipeline profile
- 静态结构 + pipeline profile + hardware
- 静态结构 + pipeline profile + hardware + history

当前 AutoKernel 单-kernel harness 里，可以把 `bench.py` 摘要当作临时局部信号。
但后面真正该测的是 pipeline profile 驱动的 prior，而不是 benchmark 指标本身。

### 2. 分支正交机制

- 无机制约束
- family-level proposal
- family-level proposal + family 去重

### 3. 历史回溯方式

- 无 insight reuse
- 最近失败回溯
- family-level insight reuse
- motif-level archive reuse

### 4. 执行约束强度

- 自由 executor
- 轻 guardrail
- 强 family guardrail

### 5. 目标粒度

- 单 kernel
- kernel region
- 多 kernel pipeline bottleneck

## 当前最值得保留的一句设计判断

如果要把 `watchman v1` 的核心压成一句话，那就是：

> `watchman` 不是一个更聪明的 executor，  
> 而是一个将 profile、pipeline bottleneck、hardware context 和历史 insight 压缩成高层分支身份的能力放大器。
