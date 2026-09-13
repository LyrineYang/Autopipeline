# AutoPipeline

**把模型实现交给 agent，在固定评测下反复修改、验证和选择，交付可复查的候选代码。**

AutoPipeline 面向 PyTorch 推理和组合模型 pipeline 的实现优化：给定硬件、
模型、输入与预算，让 agent 尝试改进实现，用实际运行结果决定是否保留。
它基于 [AutoKernel](https://github.com/RightNow-AI/autokernel) 扩展，保留原有
单 kernel 工作流，并提供覆盖整个模型实现的 pipeline 优化入口。

输入是参考代码、工作负载和执行预算；输出是候选代码、数值校验、计时、
profiler、运行报告与可重新验证的导出包。优化不保证提速；没有改进也是明确结果。

## 工作原理

```text
参考模型 + 固定输入
        ↓
建立基线，测量并生成 profiler
        ↓
agent 修改候选 → 执行评测 → 数值/性能判定 → 保留或拒绝
        ↑                         │
        └────── 在预算内继续 ──────┘
        ↓
独立的最终验证 → 报告 → 导出代码
```

- **固定评测**：参考实现、输入生成与评测器不属于默认可编辑范围。
- **真实测量**：执行模型计算，记录延迟样本和算子 profiler；不用公式模拟性能。
- **可控运行**：限定轮数、候选数、agent 调用数和超时；单 GPU 默认串行测量。
- **可追溯结果**：冻结每次运行的参考文件，保留失败日志、候选和验证记录。
- **统一引擎**：组合模型、单 kernel 和外部 benchmark 通过 task/evaluator 接口复用优化流程。

## 适合哪些场景

| 场景 | 提供什么 | 从哪里开始 |
| --- | --- | --- |
| 第一次检查产品是否能运行 | Python + PyTorch，无需模型权重 | `residual-mlp` 案例 |
| 优化注意力或多算子组合实现 | 固定形状、dtype、目标设备 | `attention` 案例 |
| 优化已有模型或业务 pipeline 的计算部分 | 自己的模型代码、权重和代表性输入 | `init --source` |
| 优化本地预训练编码器的前向计算和池化 | 本地 Transformers 模型目录 | `hf-local` 案例 |
| 比较优化 agent / 搜索策略 | 独立 benchmark 的固定题目与评测协议 | 通用 evaluator 接口；Level 5 独立维护 |
| 只优化抽取出的 GPU kernel | AutoKernel 源码工作区与 NVIDIA GPU | [原有 profile/extract/tree 流程](docs/quickstart.md#original-autokernel-path) |

当前内置评测器测量 **模型 forward，包括主机调度和设备同步**，不包含模型加载、
输入准备、网络请求或完整线上服务延迟。流式协议、多机调度等工作负载使用自定义 evaluator。

## 快速开始：不需要服务器，也不下载模型

要求 Python 3.10+。目前从源码安装，尚未发布到 PyPI；推荐独立虚拟环境。
以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[cpu]'

autopipeline pipeline examples
autopipeline pipeline init workspace/demo --example residual-mlp --device cpu
autopipeline pipeline check --spec workspace/demo/pipeline.json
autopipeline pipeline evaluate --spec workspace/demo/pipeline.json --run-dir workspace/demo/runs/baseline
autopipeline report workspace/demo/runs/baseline
```

打开 `workspace/demo/runs/baseline/view/dashboard.html` 查看结果。
这会执行真实的 PyTorch 小模型，但使用随机初始化权重；它用于检查计算和产品流程，
不代表预训练模型的业务效果，也不证明 GPU 优化收益。

只想检查配置、不安装 PyTorch：`python -m pip install .`，然后使用
`pipeline check --spec ... --no-runtime`。安装 wheel 后，pipeline 命令可以在源码目录之外使用。

## 在服务器上开始优化

先准备与 NVIDIA 驱动兼容的 CUDA PyTorch 环境。仓库的 uv 配置在 Linux 上选择
CUDA 12.8 wheel；可使用 `uv sync --extra gpu`。已有受管理 PyTorch 环境时，
安装本产品及缺少的依赖即可，不必替换服务器原有环境。

```sh
autopipeline pipeline init workspace/gpu-demo --example attention --device cuda
autopipeline pipeline check --spec workspace/gpu-demo/pipeline.json
autopipeline pipeline evaluate --spec workspace/gpu-demo/pipeline.json --run-dir workspace/gpu-demo/runs/baseline

# 在同一台执行机器上安装并登录 Codex CLI，再调用优化 agent
autopipeline pipeline run --spec workspace/gpu-demo/pipeline.json \
  --run-dir workspace/gpu-demo/runs/search \
  --executor-backend codex-app-server \
  --max-rounds 3 --children-per-expansion 2 --max-agent-calls 12

autopipeline report workspace/gpu-demo/runs/search
autopipeline pipeline export workspace/gpu-demo/runs/search --output-dir workspace/selected
```

`init`、`check`、`evaluate` 不调用模型服务。`run` 调用你配置的 agent，可能消耗
API 或订阅额度；调用次数限制不是美元金额上限。未配置 executor 会明确报错。
默认 planner/reflector 使用本地策略，也可分别配置模型后端。其他 coding agent
通过 `--executor-command` 接入，见 [运行协议](docs/agent_contract.md)。

CUDA 不可用时不会偷偷切换到 CPU。每次运行使用新目录，不覆盖之前的结果。

## 案例与模型资源

```sh
# 无需下载：注意力 → 残差 → MLP
autopipeline pipeline init workspace/attention --example attention --device cpu

# 使用自己的代码：源目录包含 reference.py，可选 candidate.py
autopipeline pipeline init workspace/custom --source /path/to/my-workload --device cuda

# 使用已有预训练编码器：安装模型加载依赖，但不自动下载权重
python -m pip install '.[cpu,models]'
autopipeline pipeline init workspace/encoder --example hf-local \
  --model-dir /path/to/local-encoder --device cpu
```

**被优化的模型**与**负责修改代码的 agent 模型**是两个概念：

| 对象 | 是否需要下载 |
| --- | --- |
| 内置 MLP / attention 工作负载 | 不需要；代码和随机初始化参数即可实际计算 |
| 自己的模型 | 已有代码与权重可以直接接入 |
| 预训练工作负载 | 需要已有本地权重；没有时才需要另行下载 |
| Codex 等优化 agent | 本产品连接其服务/CLI，不要求在本地下载 agent 权重 |

`hf-local` 使用本地文件加载，不联网补齐缺失权重，也不运行 Hub 上的自定义代码。
初始化时记录模型文件哈希；之后权重变动会在检查阶段报错。模型文件留在外部目录，
不会为每个候选重复复制。导出后需要保留相同资源，迁移路径的方法见案例文档。

[完整案例：适用场景、输入输出、下载准备与验收命令](examples/README.md)。

## 接入自己的模型

```text
my-workload/
├── reference.py   # Model、get_inputs()、可选 get_init_inputs()
└── candidate.py   # ModelNew；未提供时自动生成继承参考模型的初始实现
```

候选保留相同构造参数和 state_dict 接口。输入可以是位置参数列表或关键字字典；
输出可以是 tensor 或嵌套容器。评测器跨多个输入种子检查完整输出，拒绝非有限数、
结构/dtype 变化及超出容差的结果。浮点容差、warmup 和计时次数由固定配置指定。
组合模型可以封装在一个 `forward()` 中；其他框架或流式任务可提供自己的命令评测器。

接口、可编辑范围、计时定义、导出重测与错误排查见 [详细使用指南](docs/quickstart.md)。

## AutoPipeline 与独立 benchmark

- **产品主线 `main`**：CLI、优化 agent、通用任务接口、执行验证、预算、报告和导出。
- **Level 5**：独立 benchmark 开发线，负责题目、工作负载、基线、正确性和评分协议。
- **apimetric**：独立 benchmark 统计开发线，负责 benchmark 侧的调用量、耗时与成本记录。

Level 5 和 apimetric 不合并进产品核心。产品自身的运行预算/使用量日志仍保留，
它们与 benchmark 对实验的统计属于不同职责。Level 5 是本项目定义的扩展，
不是 upstream KernelBench 官方级别。目前其独立 harness 还需对接产品的工作区接口；
不能把内置示例视为已经跑通 Level 5。

[层次与适配边界](docs/architecture.md) · [本地分支与工作目录](docs/dev/worktree-map.md)

## 当前状态与下一步

这是可安装的 alpha 产品。CPU 数值/执行测试、真实 profiler、候选导出和安装包验证
已经建立；尚未作出目标 GPU 上的兼容性、优化成功率或加速比承诺。
当前不支持完整搜索调度状态的断点恢复，也不负责自动部署服务。

接下来的验证按会影响产品决策的顺序进行：

1. **无服务器时继续完成**：案例与本地模型接入、安装包回归，以及独立 benchmark 的
   工作区适配。Level 5 和 apimetric 的修改留在各自开发线。
2. **有一张可用 NVIDIA GPU 后**：先跑无需下载的案例，检查 CUDA、数值和 profiler；
   再让已登录的 agent 运行一轮，确认真实编辑、评测和失败恢复路径。
3. **选定业务负载后**：固定权重、输入与优化预算，重复比较基线和候选，再决定扩大
   搜索预算或改变优化策略。需要你的核心选择是目标负载和可接受的资源预算。

无需为了第一轮环境验收先准备大模型、多 GPU 或完整业务数据。

## 文档与开发

[使用指南](docs/quickstart.md) · [案例](examples/README.md) · [架构](docs/architecture.md) ·
[Agent 入口](docs/agent_entry.md) · [贡献指南](CONTRIBUTING.md) · [更新记录](CHANGELOG.md) · [安全说明](SECURITY.md)

AutoPipeline 保留 AutoKernel 的 MIT 许可和来源信息；上游作为实现基础与参考，
产品开发与发布面向自己的仓库。
