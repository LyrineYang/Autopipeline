# Tree Planner 共识记录

这份文档记录当前关于 tree-search 中 `planner` 角色的共识。
目标不是给出最终实现细节，而是把已经说清楚的判断固定下来，避免后续讨论反复回到“现在系统到底怎么工作”这个问题。

## 当前代码状态补记

为了让这份共识文档和当前代码保持一致，先补一条当前已经完成的阶段状态：

- 运行时命名已经统一到 `planner`，旧的 `watchman` 不再是当前运行时名词
- framework core 已经基本从 AutoKernel 单文件 worldview 里退出来：
  - task protocol 不再假设只能改一个 `kernel.py`
  - branch-local loop 支持 task 自定义 checkpoint / restore 和 execution surface
  - branch identity 已经带 `scope / target / verification_intent`
- sensing 已经同时支持：
  - built-in inspection probes
  - built-in `command` / `python` probes
  - built-in `theoretical_limit_inputs`，用于给 agent 提供稳定的理论上限输入
- 理论上限或 roofline 计算当前约定为两段式：
  - sensing / task 提供稳定输入
  - 具体计算脚本由 agent 用 `python` probe 编写和执行
- 文档已经分成人类入口和 agent 入口：
  - `README.md` 继续给人类
  - `docs/agent_entry.md` 和 `docs/agent_contract.md` 负责 framework-driven agent

当前仍然没有变的边界也要保留清楚：

- bundled task 仍然是 AutoKernel-specific
- `tree_optimize.py` 仍然是当前 shipped 的 AutoKernel CLI
- pipeline task 和 pipeline evaluator 仍然没有作为默认实现进入仓库

## 背景

当前仓库已经有一套可运行的 tree-search 适配层。
它已经能做这些事：

- 分支展开
- 前沿保留
- 剪枝
- 节点隔离
- evaluator 驱动的验证
- insight 回流

但它还没有把“分支方向生成”做成一等能力。

现在的核心问题不是 tree 有没有搜索控制，而是：

- 不同分支为什么朝不同方向走
- 这些方向是不是足够有信息量
- 这些方向有没有真正利用 profiling / hardware context
- 这些方向有没有真正对应 pipeline 的主要 bottleneck

## 当前实现的事实

### 1. 搜索控制已经存在

当前 tree engine 负责的是搜索控制，而不是搜索内容本身。

也就是说，它负责：

- 展开多少个分支
- 哪些分支进入下一轮
- 哪些分支被剪掉
- 哪个节点当前最好
- 节点结果怎样写入状态和事件日志

它不负责回答：

- 这个节点下一步该试什么思路
- 哪几条分支在优化语义上应该彼此不同

### 2. 当前分支方向主要来自两种来源

第一种来源是内置 seed hypotheses。

如果没有外部 planner 命令，系统会使用一组通用方向模板，例如：

- 调 launch / tile
- 改 memory access
- 降低局部开销
- 尝试一个局部算法变体

这些方向是通用启发式，不是 profile 驱动的细分策略。

第二种来源是外部 planner 命令。

如果配置了 planner command，那么真正的分支方向由 planner 输出。
tree adapter 只负责把上下文打包给 planner，再接收 planner 返回的 hypotheses。

### 3. profile 目前的作用是“选目标”，不是“定方向”

当前 profile / extract 流程会决定：

- 哪些 kernel 值得优先优化
- 每个 extracted kernel 的类型
- 模型相关 shape
- 这个 kernel 在总 GPU 时间中的占比

这些信息会进入 extracted kernel 文件本身，也会影响当前单-kernel harness 的测试形状。

但现在这些 profiling 信息还没有系统性进入 proposal 阶段。

换句话说：

- profile 现在负责“优化哪个 kernel”
- 当前单-kernel harness 里，local evaluator + reflect 会影响下轮方向
- planner 还没有成为一个真正由 profile / pipeline bottleneck 驱动的角色

## 已达成的共识

### 用户侧判断

用户的核心判断是：

> 搜索控制负责分支展开、前沿保留、剪枝、记录哪条分支更值得继续。  
> 现在的分支展开里，不同分支的方向是如何确定的，是关键问题。  
> 需要把这部分能力接入 `profile.py`，利用 profile 提供的信息来决定分支的走向。  
> 同时，tree search 和 agent 的 subagent 机制非常匹配。

这个判断是当前后续工作的核心输入。

### 当前系统状态判断

当前实现里：

- tree adapter 已经有“搜索控制”
- 但“方向生成”还主要依赖通用模板或外部 planner
- profile 还没有直接决定每一轮 proposal 的方向

补充当前实现边界：

- 代码里已经加入了轻量的 `RegionGraph` 和 `BottleneckCard`，可以承载 pipeline 风格的 scope、region、upstream/downstream、bottleneck hints
- 但当前 `AutoKernelTask` 仍然只是单-kernel harness，只编辑 sandbox-local `kernel.py`
- 所以今天这条实现路径里，pipeline 风格输入已经可以进入 diagnosis，但 response family 仍然必须裁剪到 kernel-local family

所以目前的短板不在“有没有树”，而在“树的每个分支是否足够有根据”。

### 共同认可的方向

我们已经形成的共识是：

- 需要一个类似 planner 的角色，或者说一个强化版 profiler
- 这个角色的任务不是执行代码修改，而是生成更有信息密度的分支方向
- 它应该把 profile、pipeline bottleneck、hardware context、历史 insight 组织成更适合 proposal 的输入
- 这部分很可能比继续单纯改 beam search 策略更重要

## 为什么需要 Planner

如果只靠通用 seed hypotheses，tree 很容易退化成：

- 几条方向名字不同，但本质很像
- 没有真正结合当前 kernel 的瓶颈
- 没有结合显卡和运行环境特征
- 没有利用 profiling 结果对分支进行约束

这会直接影响 tree 的有效分叉质量。

也就是说，真正限制搜索效率的往往不是：

- beam width 太小
- round 数不够

而是：

- proposal 本身信息不足
- agent 没拿到足够好的优化 context

## Planner 应该提供什么信息

我们当前共识里，planner 应该逐步成为“方向生成器”，而不是另一个执行 agent。

它至少应该向 proposal 阶段提供这些信息：

- 当前 kernel 的 profile 背景
- 该 kernel 在整模型里的重要性
- 该 kernel 的主要 shape 和附近 shape
- 已知 bottleneck 更偏 compute、memory、sync、launch，还是多 kernel 边界问题
- 当前 pipeline profile 暴露出的阶段耗时、硬件利用、等待与衔接问题
- 当前设备相关上下文，例如显卡类别和可利用能力
- 已尝试方向和最近失败模式
- 哪几类方向应该刻意保持差异化

用户特别提到的“显卡 util 这类优化 context”，就是这类 planner 输入的一部分。

## Planner 和 Profile 的关系

当前共识不是简单“重写 profile.py”，而是：

- profile 提供原始分析信息
- planner 把这些信息转成 proposal 可用的分支方向

所以更自然的理解是：

- `profile.py` 负责测和提取事实
- planner 负责把事实转成“下一步应该探索哪些方向”

后面具体实现时，可以是两种路径：

1. 在 `profile.py` 基础上增强，直接输出更适合 planner 使用的结构化信息
2. 保持 `profile.py` 负责原始分析，新增单独 planner 层来消费 profile + pipeline bottleneck analysis + history

当前共识更偏向第二种：planner 是一个独立角色，但要深度消费 profile 结果。

这里还要补一条新的共识：

- `bench.py` 很适合当前 AutoKernel 单-kernel harness 的 evaluator 角色
- 但它不应该被上升为通用 framework 的核心 proposal 信号
- 如果目标扩展到任意 pipeline，真正应该进入 proposal 的是 pipeline bottleneck profile 和 hardware profile

## Planner 和 Subagent 的关系

tree search 和 subagent 机制是匹配的，这一点已经明确认可。

原因很简单：

- planner 负责分方向
- subagent 负责在某一条方向上做局部执行
- tree controller 负责保留谁、剪掉谁

这个分工比“一个强 agent 自己在长上下文里又想方向又写代码又评价自己”更稳定。

一个更理想的结构会是：

- planner 先产出几条差异足够大的方向
- 每条方向交给不同 subagent 在各自 sandbox 中执行
- benchmark / verifier 独立裁决
- 搜索控制层只做保留、剪枝、继续扩展

## 目前最重要的结论

当前 tree adapter 已经解决了“怎么管树”的大半问题。

下一阶段真正决定效率上限的，不是继续堆更多搜索控制逻辑，而是把下面这件事做好：

> 让 proposal 不再只是通用启发式，而是由 profile、pipeline bottleneck、hardware context 和历史 insight 共同驱动。

这就是 planner 要解决的核心问题。

## Planner / Sensing 简版共识

这部分只记录当前已经落实到代码里的简版共识，避免后续继续把 `planner`、`probe`、`verifier` 混成一层。

### 1. Planner 是高层 agent，不是规则打包层

当前共识已经明确：

- planner 吃事实、规则、历史和感知结果
- planner 决定方向、scope、分支数量和探索强度
- executor 负责沿着固定 branch identity 做局部实现

所以 system prior 只保留成 planner 的输入，不再让 task 代码替 planner 做高层判断。

### 2. 感知层只做三件事

当前感知层的职责被刻意收窄成：

- 最小被动观测
- 运行 agent 定义的诊断请求
- 统一记录事件和 probe 结果

也就是说，probe 的语义已经固定成：

> probe 是 agent 定义的诊断请求，感知层只负责受控执行和回传。

这避免了把感知层继续做成“预定义工具集合 + 固定控制流”。

### 3. 当前 task probe 的落点

为了保持代码简单，domain-specific probe 不继续放进 sensing core，而是继续落在 task 的 `handle_probe(...)`。

当前 `AutoKernelTask` 已经接了这些最小 probe：

- `gpu_status`
- `compile_smoke`
- `compile_report`
- `python_experiment`
- `ncu_profile`
- `nsys_profile`
- `stage_summary`

补充当前已经形成的五类 probe 视角：

- 硬件画像：`hardware_profile`
- 软件策略审计：`software_audit`
- 运行时拆解：`runtime_breakdown`
- 硬件利用：`ncu_profile` / `nsys_profile`
- 主动实验：`python_experiment`

这些 probe 都是薄封装：

- 感知层不理解 kernel / pipeline 语义
- task probe 只负责调用现有工具或脚本
- 复杂实验内容尽量由 agent 在 request 里决定

### 4. 当前刻意保留的简化

这一阶段有几条刻意不做重的选择：

- 不在 sensing core 里继续加新角色或新协议层
- 不把 `ncu/nsys/trace` 解析成很重的统一 schema
- 不让 probe 结果直接进入 acceptance logic
- 不让 planner probe 直接跑在 repo root，而是绑定当前 branch workspace

当前这版的目标不是“完整 pipeline 反馈系统”，而是先让主动感知闭环可用，同时保持实现足够薄。

### 5. 现在的 verifier 边界

当前 sensing manifest 里的 `verifier` 还是一个轻量摘要器：

- 统计最近 probe 的完成/失败情况
- 给 planner 一个压缩后的诊断健康视图

它还不是 pipeline-level verifier。

真正的 pipeline verifier 仍然应该建立在后续更完整的 trace / stage timing / end-to-end objective 之上，而不是提前塞进 sensing core。

## 更强的主张：为什么 Kernel 优化比一般 Agent 任务更适合 Tree Search

如果只说“planner 负责 exploration，agent 负责 exploitation”，这个主张还太弱。

更强、更贴近当前项目的说法应该是：

> Kernel 优化天然更适合层级化、经典、简单的 tree-search 结构。  
> 原因不是 kernel 更容易，而是它的搜索对象更规整、反馈更强、验证更硬、层级更自然。  
> 因此，随着 agent 本身不断变强，这套 tree 结构不会过时，反而会越来越有价值，因为它能持续把更强的局部能力接入到稳定的全局搜索框架里。

这个主张和一般 agent tree search 的差别在于：

- 一般 agent 任务往往目标模糊、环境变化快、状态隐藏多、反馈稀疏
- kernel 优化则有更强的外部 verifier、更明确的目标、更局部的改动边界
- 因此这里更适合保留清晰的 hierarchy，而不是把所有智能都压进一个大 agent

换句话说，我们不是在说：

- “tree search 可以用在 kernel 优化上”

而是在说：

- “kernel 优化是最适合 tree search 的一类 agent 优化任务之一”

## 为什么 Kernel Search 和通用 Tree Search 不一样

当前项目里，kernel tree search 和一般语言任务 / 开放环境任务的 tree search 至少有五个本质差异。

### 1. 节点不是“thought”，而是“优化假设”

在很多通用 tree-search agent 里，节点更像一个推理片段、行动片段、或中间文本状态。

但在 kernel 优化里，一个节点更自然地对应：

- 一个明确的优化假设
- 一个局部实现方向
- 一个可验证的候选 kernel

这使得树的语义更稳定，也更容易做剪枝和归档。

### 2. 验证器比一般 agent 任务更强

这里有固定 evaluator、正确性检查、性能指标、roofline / bottleneck 信号。

这意味着：

- 节点价值不是靠 agent 自述决定
- 失败也不是黑箱失败
- 搜索可以绑定到更硬的环境反馈

这类任务更接近“程序优化搜索”，而不是“开放式智能行为”。

### 3. 层级天然存在

kernel 优化天然就有一个层级结构：

- 先决定优化哪个 kernel
- 再决定哪个优化家族值得试
- 再决定这条家族下的具体实现分支
- 最后再做局部修补和 exploit

这比很多通用任务更适合树结构，因为 hierarchy 本来就在任务里，而不是人为强塞进去。

### 4. 分支差异可以做成结构性的，而不是文案性的

很多通用 agent tree search 的多样性最后退化成“说法不同”。

但 kernel 优化的分支差异可以更结构化：

- memory access 路线
- occupancy / launch 路线
- async / latency hiding 路线
- precision / datatype 路线
- instruction / compute 路线
- control flow / indexing 路线

这类差异是真正的优化家族差异，不是语言表述差异。

### 5. 强 agent 的进步更容易被这套框架吸收

如果 agent 变强，它首先提升的是：

- 局部实现质量
- 单步 bug 修复能力
- 受限上下文下的代码变更质量
- 对反馈的吸收能力

这些提升天然能直接增强 tree 中每个节点的 exploitation 质量。

也就是说，tree 结构不需要随着 agent 变强而消失；相反，它会成为吸收更强 agent 能力的稳定外壳。

## 更具体的系统观点

我们后续要构建的，不应只是“一个会分叉的 agent”。

更合理的系统抽象是：

- planner 决定哪几类方向值得探索
- subagent 在单个方向内做局部实现
- benchmark / verifier 做外部裁决
- tree controller 负责预算分配、前沿保留、剪枝
- archive / memory 负责保存成功模式和失败模式

如果把目标扩展到 pipeline 级优化，这个系统观点还要再往前推一步：

- 先理解 pipeline 的 bottleneck 区域
- 再判断这是不是单 kernel 问题
- 再决定是进入 kernel-level search，还是进入 fusion / overlap / stage-reordering 这类 pipeline-level search

这个结构的意义在于：

- exploration 被显式管理
- exploitation 被交给更强的 agent
- judge 不与 executor 混在一起
- 历史不会只停留在日志里

## 我们真正该 claim 什么

一个更有力度的 claim 可以是：

> 对 kernel 优化这类“局部可编辑、全局可验证、反馈强、层级天然存在”的任务，  
> 简洁的层级化 tree search 比“大 agent 长对话反复修补”更稳、更可扩展，也更能持续吸收未来 agent 能力进步。

还可以再具体一点：

> 在 kernel 优化中，性能提升的瓶颈往往不在更深的单链推理，而在更高质量的 hypothesis generation、branch diversity、credit assignment 和 archive reuse。  
> 因此，planner + tree + verifier + subagent 的结构，比单 agent 连续 revision 更接近正确的系统形态。

## 需要新增的研究方向：从 Kernel 到 Pipeline

我们现在的实现主要验证的是：

- tree search 能否在单-kernel AutoKernel harness 里稳定工作
- `planner` 能否给单个 kernel 提供更强的高层先验

但后面的目标不应停在这里。

如果要做一个更通用的优化 framework，还需要补上：

### 1. pipeline bottleneck 的抽象

需要能回答：

- 慢的是哪个阶段
- 慢是因为单 kernel，还是多个 kernel 之间的边界
- 问题更像算子本身、layout 传播、fusion 机会、launch 开销，还是调度与 overlap 不足

这里新的关键共识是：

- bottleneck 表示不能只停在“哪个 kernel 最慢”
- 更重要的是把 observation、diagnosis、treatment 分层

也就是：

- 先收集症状
- 再判断病因
- 最后决定治疗方案

这个层次如果不分开，proposal 很容易从零散 signal 直接跳到低质量方向。

### 2. pipeline-level branch family

除了单-kernel family，后面还需要 pipeline family，例如：

- kernel fusion
- layout propagation
- stage reordering
- launch consolidation
- overlap / pipelining
- communication / compute overlap

并且后面要允许联合方案，而不是只做单点方案。

也就是说，branch card 后面应该支持：

- primary family
- secondary family

前者表示主治疗方向，后者表示联合 bottleneck 下允许联动的副方向。

### 3. hardware profile 的一等地位

我们已经认可硬件信息很重要，但后面它不该只是补充背景，而应该成为主 prior 之一。

因为通用 framework 里，proposal 是否合理，往往首先取决于：

- 当前设备到底容易被什么限制
- 哪类方向在这块卡上值得试
- 哪类方向虽然局部看起来有收益，但在真实 pipeline 上不稳定

### 4. evaluator 和 prior 的分离

后面必须一直保持这个区分：

- evaluator 用来裁决这条路径是否更好
- prior 用来帮助 proposal 决定先试哪类路径

对今天的 AutoKernel adapter，`bench.py` 可以做 evaluator。
但对更一般的 framework，prior 不能直接退化成“读 benchmark 指标再猜下一步”。

## 下一步实现顺序

当前最合理的顺序应该是：

1. 先定义 pipeline bottleneck schema
2. 再定义 diagnosis -> treatment family 的映射
3. 再把联合 bottleneck 的联合方案放进 branch card
4. 最后再让 `planner` 真正按这套 schema 产出 proposal prior

这四步里，当前代码只完成到一个更早的中间状态：

- schema 的壳子已经接进 `planner`
- 单-kernel harness 已经能读轻量 pipeline 风格 hints
- 但真正的 pipeline profiler / task 还没有接上

这里“让真正的 pipeline profiler 把这些轻量字段喂进来”的意思是：

- 不是让 `bench.py` 再承担更多 prior 角色
- 而是让未来的 pipeline profiler / trace 分析阶段产出少量稳定字段，例如 `optimization_scope`、`pipeline_region_label`、`upstream_regions`、`downstream_regions`、`profile_bottlenecks`
- 再由 `planner` 消费这些字段，生成更合理的 diagnosis 和 branch identity

这个顺序的好处是：

- 先冻结高层概念
- 再让实现跟着概念走
- 避免一开始就把 proposal 逻辑写成一堆混杂规则

## 对 Exploration / Exploitation 的更高层理解

在这个问题里，exploration 和 exploitation 不是简单的参数平衡问题，而是分工问题。

### Exploration 主要失败在这些地方

- proposal 缺乏足够强的先验
- 分支看起来不同，实质上还是同一路
- 单次分数主导剪枝，导致过早塌缩
- 失败经验没有进入下一轮 proposal

### Exploitation 主要失败在这些地方

- executor 没有忠实执行分支身份
- 局部实现能力不足
- 节点粒度过大，导致 credit assignment 混乱
- benchmark 反馈没有被稳定吸收

所以更高层的结论不是：

- “要平衡 exploration 和 exploitation”

而是：

- “要把 exploration 做成 planner / archive / diversity 机制”
- “要把 exploitation 做成 subagent 的局部实现能力”
- “要把两者之间的信用分配交给 verifier + memory，而不是交给一个 agent 自己解释”

## 下一阶段最值得研究的抽象问题

如果从研究视角继续推进，这里真正有价值的问题不是“beam 再怎么调”，而是：

### 1. Search Prior

proposal 的先验到底从哪里来：

- profile
- benchmark
- hardware context
- archive
- 失败模式

### 2. Diversity Mechanism

如何定义“真正不同的分支”：

- 不是文本不同
- 而是优化家族不同、资源使用模式不同、预期瓶颈变化不同

### 3. Credit Assignment

一次失败到底意味着什么：

- 方向错了
- 实现差了
- 节点粒度错了
- benchmark 有噪声
- verifier 没提供足够细的反馈

### 4. Compute Allocation

预算如何分配：

- 给全新方向
- 给已有潜力方向深入 exploit
- 给边缘分支复验
- 给历史优质方向做重组

## Research Agenda

为了把后续讨论抬到更高层，建议调研不只看 agent 本身，而是按四条主线展开。

### 1. Tree / Planning 框架

- Tree of Thoughts (2023): https://arxiv.org/abs/2305.10601  
  看的是“节点从 token 变成可搜索的 thought”。
- Language Agent Tree Search (2023): https://arxiv.org/abs/2310.04406  
  看的是 reasoning、acting、planning 如何统一。
- Reasoning with Language Model is Planning with World Model (2023): https://arxiv.org/abs/2305.14992  
  最值得吸收的是 world model 视角。

### 2. 多样性与 Archive

- Illuminating Search Spaces by Mapping Elites / MAP-Elites (2015): https://arxiv.org/abs/1504.04909  
  这类方法提醒我们：不是只找一个最好点，而是维护一张高质量解的分布图。
- Diverse Beam Search (2016): https://arxiv.org/abs/1610.02424  
  适合研究“伪多样化”为什么经常出现。
- Novelty Search and the Problem with Objectives (2011): https://doi.org/10.1007/978-1-4614-1770-5_3  
  适合研究为什么单一分数会把搜索带进死胡同。

### 3. 失败记忆与反馈吸收

- Reflexion (2023): https://arxiv.org/abs/2303.11366  
  看失败如何变成语言级可复用记忆。
- Self-Evaluation Guided Beam Search for Reasoning (2023): https://arxiv.org/abs/2305.00633  
  看 judge 的作用，而不是只看 generator。

### 4. Kernel-Specific Test-Time Search

- Stanford CRFM / Scaling Intelligence Fast Kernels blog (2025): https://crfm.stanford.edu/2025/05/28/fast-kernels.html  
  一个关键启发是：高性能 kernel 往往出现在后期轮次，而且高质量解会聚集到少数 recurring optimization strategies。
- AutoKernel: https://github.com/RightNow-AI/autokernel  
  它说明固定 verifier + 长时间 revision loop 是有效基线。

我们真正想做的，不是简单站在这两者中间，而是把它们组合成：

- AutoKernel 的稳定 benchmark / verifier /工程约束
- Fast Kernels 的 hypothesis-driven branching
- 再加上 planner、archive、subagent、credit assignment

## 当前最值得保留的一句系统判断

如果要把这份共识压成一句最关键的话，那就是：

> Kernel 优化不是一个“需要更自由 agent”的问题，而是一个“需要更强 search prior、更硬 verifier、更清晰 hierarchy、更可复用 archive”的问题。

这也是后续 planner 设计最该服务的核心判断。

## 还应继续探索的 Insight

前面的共识已经足够作为研究起点，但还不够成为最终的研究框架。
如果要把 planner 做成真正有学术价值的一层，我们还应该继续追问下面这些问题。

### 1. Kernel 任务为什么是“强 Verifier 任务”

这里的关键不是简单说“有 benchmark”。

更重要的是：

- verifier 是不是足够硬
- verifier 输出是不是足够细
- verifier 能不能把错误、退化、波动分成不同类型

如果 verifier 足够强，那么系统设计就不应该把太多判断交给 agent 自己解释，而应该更多依赖外部搜索与裁决。

这意味着一个更深的问题：

- 强 verifier 任务是否天然更适合外部 tree search，而不是内部长链反思

### 2. Proposal 的先验到底来自哪些层级

现在我们已经识别出几个来源：

- profile
- benchmark
- hardware context
- history / archive

但更高层的问题是：

- 哪些是静态先验
- 哪些是动态先验
- 哪些是任务级先验
- 哪些是设备级先验
- 哪些是分支级先验

如果这个问题说清楚，planner 就不再只是“多喂一点信息”，而是一个真正的多层先验融合器。

### 3. 多样性不应按文本定义，而应按优化机制定义

这是一个非常重要的方向。

在 kernel 优化里，所谓多样性不该只是：

- prompt 不同
- 文字表述不同
- 假设句子不同

而应该是：

- memory hierarchy strategy 不同
- parallelization strategy 不同
- compute / instruction strategy 不同
- data type / precision strategy 不同
- control-flow / indexing strategy 不同

也就是说，我们真正想保留的是 mechanism diversity，而不是 wording diversity。

### 4. Failure 不是负样本，而是结构化搜索信息

很多系统把失败只看成“这条路不行”。

但在 kernel 优化里，失败经常带有很强的结构：

- correctness fail
- compile fail
- timeout
- occupancy collapse
- bandwidth 没提升
- compute utilization 没提升
- 近邻 shape 不稳定

这说明 failure 不应该只是日志，而应该被编码成下一轮搜索的约束。

真正值得研究的问题是：

- failure taxonomy 能不能直接决定下一轮 exploration 的质量

### 5. 强 Agent 在这里更像局部优化器，而不是全局搜索器

这可能是对 agent 能力边界非常重要的判断。

在 kernel 优化里，强 agent 最自然的长处往往不是：

- 宏观规划整棵树
- 自发设计高质量 search policy

而是：

- 在单个节点内做高质量局部改写
- 吸收结构化反馈
- 沿着单一假设做受限实现
- 在局部 exploit 上做得越来越强

这意味着：

- agent 更像 node-level optimizer
- planner / controller 更像 tree-level optimizer

### 6. Compute Allocation 应该成为研究对象，而不是超参数

在 tree search 里，预算分配不能只是工程调参。

真正值得研究的是：

- 预算该给全新方向，还是现有强分支
- 哪些分支值得复验
- 哪些分支值得深挖 exploit
- 哪些低分但高新颖度分支该被保留

也就是说，compute allocation 本身就是 search intelligence 的一部分。

### 7. Archive 的意义不只是记忆，而是提炼可复用的 Optimization Motifs

这个方向和 fast kernels 的观察高度一致。

如果高性能 kernel 会不断聚集到少数 recurring optimization families，那么 archive 的价值就不只是“记住试过什么”，而是：

- 提炼 recurring motifs
- 让 planner 在 proposal 时复用这些 motifs
- 让未来分支不必从零开始

这意味着 archive 更像 motif library，而不是 experiment log。

### 8. Hierarchy 可能本身就是这类任务的 Inductive Bias

这是当前最值得深挖、也最有潜力成为核心理论主张的方向。

kernel 优化并不是事后被树结构“包装”起来的任务。
它本身就天然带有层级：

- 先决定优化哪个 kernel
- 再决定关注哪类瓶颈
- 再决定走哪一类优化机制
- 再决定该机制下的局部实现变体
- 最后才是局部修补和 exploit

这意味着 hierarchy 不是一个工程技巧，而可能是这个任务本身的 inductive bias。

如果这个判断成立，那么一个很强的研究问题就出现了：

- agent 为什么在符合任务层级结构的系统里更稳定
- agent 为什么在缺少 hierarchy 的长上下文单链修补中更容易漂移、塌缩、重复

这可能会引出一个更一般的判断：

> 对于局部可编辑、全局可验证、反馈强、天然存在多层决策结构的任务，  
> hierarchy 不是为了帮助 agent 才加上的外部框架，  
> hierarchy 本身就是任务结构的一部分，系统必须尊重它。

如果后面要把 planner 做成一个真正有研究价值的模块，这一点很可能应当成为理论中心。

## 关于 Hierarchy 对 Agent 的更深问题

既然 hierarchy 被认为是最值得深入的方向，那么后续值得专门追问这些问题：

### 1. Hierarchy 对 agent 到底意味着什么

它可能意味着：

- 降低单个上下文内需要同时考虑的决策维度
- 将 exploration 和 exploitation 放到不同层
- 让失败信用分配更清楚
- 让 memory 以更可压缩的方式组织
- 让更强的 agent 能力自然落到局部节点里

### 2. Hierarchy 是否改变了 agent 的最优角色

在无层级结构里，agent 往往被期待同时承担：

- 规划
- 执行
- 评估
- 反思

但在层级化结构里，更自然的分工可能变成：

- planner 负责高层分支设计
- subagent 负责节点内实现
- verifier 负责裁决
- archive 负责跨轮记忆

这说明 hierarchy 不只是缩短 prompt，而是重新定义 agent 在系统中的职责。

### 3. Hierarchy 是否让“更强的 agent”更可持续被系统吸收

如果 agent 持续进步，最先提升的通常是局部 exploitation 能力。

那么层级化系统的优势在于：

- 它不需要推翻整体结构
- 只需要把更强的局部 agent 接到节点执行层
- 整体 search prior、credit assignment、archive reuse 仍保持稳定

这意味着 hierarchy 可能是 agent 系统长期演化中的“稳定接口”。

## 对下一阶段 Planner 实现的直接启发

如果准备使用一个 agent 来实现 planner，那么它不应该只是：

- 再写一个会输出 hypotheses 的脚本

而应该被视为：

- 负责融合多层先验的上层角色
- 负责保持 mechanism diversity 的角色
- 负责把历史失败压成约束的角色
- 负责在 hierarchy 的高层定义下一轮 exploration 结构的角色

换句话说，planner 的价值不在“生成更多分支”，而在：

> 生成更有根据、更有结构差异、也更尊重任务层级的分支。

## 后续讨论建议

接下来如果继续推进 planner，建议按这个顺序讨论：

1. planner 的输入到底有哪些，哪些来自 profile，哪些来自 bench，哪些来自历史 insight
2. planner 的输出格式是什么，怎样保证不同分支真正不同
3. planner 是接在现有 `profile.py` 后面，还是单独做一层
4. planner 和 subagent 的配合方式怎样落到 tree adapter 的命令接口上
5. 哪些 hardware context 值得纳入，哪些会引入太多噪音

在这些问题明确之前，不宜过早把 planner 直接实现成又一个大而全的 agent。

## 现在的 Hierarchy（简版）

当前实现刻意保持轻层级。

- 决策层：`planner` 内部用 `RegionGraph -> BottleneckCard -> Branch Card` 组织思考。
- 运行层：事实输入 -> `planner` -> tree controller -> executor -> evaluator / verifier。
- `RegionGraph` 只回答“问题在哪里”，不做独立控制。
- `BottleneckCard` 只回答“问题是什么、先试哪类方案”，不保存长日志。
- prompt 只暴露 focus region 和少量 nearby regions，不把整张图灌给模型。

当前已经做出的关键选择：

- `planner` 只冻结高层方向，不写代码。
- `AutoKernelTask` 仍然是单-kernel harness，只编辑 sandbox-local `kernel.py`。
- pipeline 风格 hints 已经可以进入 diagnosis，但 response family 仍然裁剪到 kernel-local family。
- benchmark 在当前 harness 里只是局部 evaluator，不是未来通用 framework 的核心 prior。

## Pipeline Baseline 还缺什么

这里只保留结论，不重复完整列表。

当前判断不变：

- 新的 pipeline baseline 还缺真正的 pipeline profiler、pipeline task、scope router、pipeline evaluator / verifier
- 这些条件同时满足后，才能说 pipeline optimizer baseline 完成

完整的缺口定义、完成条件、实验矩阵和阶段实验方案，统一以下面这些章节为权威版本：

- [新 Baseline 什么时候算完成](#新-baseline-什么时候算完成)
- [后续实验矩阵](#后续实验矩阵)
- [后续实验方案](#后续实验方案)

## 后续实验矩阵（简版）

这里也只保留引导，不再重复完整实验矩阵。

建议仍然按阶段做实验，但详细主轴和 phase 设计统一以下面两个章节为准：

- [后续实验矩阵](#后续实验矩阵)
- [后续实验方案](#后续实验方案)

## 当前 Hierarchy 总结

现在的 hierarchy 分成两类：

### 1. 决策层级

这部分主要存在于 `planner` 内部，不被拆成很多运行时角色。

- `RegionGraph` 回答“问题在哪里”
- `BottleneckCard` 回答“问题是什么”
- `Branch Card` 回答“先试哪类方案”

这里的关键选择是：

- 让 hierarchy 存在于决策逻辑里
- 不把它扩成很多显式 agent
- prompt 里只暴露一个 focus region 和少量 nearby regions

### 2. 运行层级

运行时角色仍然保持很少：

- 上游事实输入：今天是 extracted kernel + hardware + local evaluator + history，未来可以换成真正的 pipeline profiler / trace
- `planner`：压缩高层先验
- tree controller：分叉、保留、剪枝、记状态
- executor：做一次局部实现
- evaluator / verifier：做外部裁决

这里的关键选择是：

- 不新增 observation agent / diagnosis agent / treatment agent
- 不新增独立的 region-graph controller
- 不做复杂长期 memory 服务

## 当前已经做出的设计选择

为了保持简洁，我们已经明确做了这些选择：

1. `RegionGraph` 是轻量对象，不是复杂图系统。
2. 每个 region 一个 card 就够，先不做更复杂的 region program。
3. `planner` 只负责冻结高层方向，不负责写代码。
4. 当前 `AutoKernelTask` 仍然是单-kernel harness，只编辑 sandbox-local `kernel.py`。
5. 当前代码已经允许 pipeline 风格 hints 进入 diagnosis，但 response family 仍然裁剪到 kernel-local family。
6. benchmark 在当前 harness 里是局部 evaluator，不是未来通用 framework 的核心 prior。
7. 外部 planner / executor / reflector 都是可选的，tree adapter 本身先保证结构闭环。

## 距离 Pipeline Optimizer 的完整 Baseline 还差什么

如果目标从单-kernel tree harness 走到真正的 pipeline optimizer，当前还缺五样东西：

### 1. 真正的 Pipeline Profiler

不是今天这种 extracted kernel 上的局部信号，而是能稳定产出这些轻量字段的上游阶段：

- `optimization_scope`
- `pipeline_region_label`
- `upstream_regions`
- `downstream_regions`
- `profile_bottlenecks`

### 2. Pipeline Task

当前 `AutoKernelTask` 只能改单文件 `kernel.py`。
真正的 pipeline optimizer 需要新的 task，至少能表示：

- kernel-local edit
- region-local edit
- cross-kernel edit

### 3. Kernel / Region / Pipeline 路由

现在还没有一个稳定路由器来判断：

- 这是单 kernel 问题
- 这是 region 问题
- 还是应该进入 pipeline-level search

### 4. Pipeline Evaluator / Verifier

当前 `bench.py` 和 `verify.py` 足够支撑单-kernel harness。
但新的 baseline 需要：

- stage / region latency
- pipeline bottleneck 改善
- end-to-end correctness 和端到端收益

### 5. Pipeline-Level Treatment Family 的真正执行面

今天文档里讨论了：

- fusion
- layout propagation
- overlap / pipelining
- launch consolidation

但当前还没有能真正执行这些方案的 task 和 edit surface。

## 新 Baseline 什么时候算完成

不是“做完所有研究问题”才算完成。
新的 pipeline baseline 在下面这些条件同时成立时，就算完成：

1. 上游 profiler 能稳定产出轻量字段，而不是长日志。
2. `planner` 能消费这些字段，稳定生成 `RegionGraph + BottleneckCard + Branch Card`。
3. 系统能明确区分 kernel / region / pipeline 三种 scope。
4. 不同 scope 能进入对应的 task，而不是都挤进单文件 `kernel.py`。
5. 至少有一条 pipeline evaluator 路径能判断“这条 branch 是否真的改善了 pipeline bottleneck”。

达到这五条，就可以说“新的 pipeline optimizer baseline 完成了”。

archive、复杂剪枝、cost model、motif reuse 都可以放到 baseline 之后。

## 后续实验矩阵

为了避免矩阵无限膨胀，后面建议先固定五条主轴。

### 1. Prior 来源

- 静态结构
- 静态结构 + pipeline profile
- 静态结构 + pipeline profile + hardware
- 静态结构 + pipeline profile + hardware + history

### 2. 优化 Scope

- kernel
- region
- pipeline

### 3. Branch Identity 强度

- 自由 hypothesis
- family-only
- family + primary / secondary family
- family + guardrail + avoid list

### 4. 执行面

- 单文件 kernel edit
- region-local edit
- cross-kernel / pipeline edit

### 5. 裁决信号

- local micro benchmark
- stage / region latency
- end-to-end pipeline speedup

## 后续实验方案

建议按阶段做，而不是一上来全因子展开。

### Phase 0: 保留当前基线

对照组：

- 原始 AutoKernel 长循环
- 当前 single-kernel tree adapter

目的是保留一个稳定的出发点。

### Phase 1: 验证 Prior 是否真的提升 Proposal 质量

先只比较 Prior 来源这一条轴。
看：

- time-to-first-improvement
- valid branch rate
- branch collapse rate

### Phase 2: 验证 Scope 路由是否值得

比较：

- 把所有问题都塞进 kernel search
- 先做 bottleneck diagnosis，再路由到 kernel / region / pipeline

看：

- diagnosis-to-treatment hit rate
- fixed budget 下的最好收益

### Phase 3: 验证 Pipeline Family 是否必要

只在有 pipeline task 之后做。
比较：

- kernel-only families
- kernel + pipeline families
- primary / secondary family

看：

- 是否更快找到有效方向
- 是否减少重复尝试
- 是否提升端到端收益

### Phase 4: 验证 History / Archive 是否值得

最后再比较：

- 无 history
- 只用最近 insight
- history + motif / archive

看：

- proposal 质量是否继续提升
- 是否减少重复失败

## 当前最务实的下一步

如果按“先把 baseline 做出来”这个目标推进，最直接的顺序还是：

1. 做真正的 pipeline profiler 轻量字段输出
2. 做 scope router
3. 做独立的 pipeline task
4. 接 pipeline evaluator / verifier
5. 再跑 Phase 1 和 Phase 2 实验

这五步做完，新的 baseline 才算真正从单-kernel tree harness 走到 pipeline optimizer。
