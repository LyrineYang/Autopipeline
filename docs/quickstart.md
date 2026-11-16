# Quickstart

先在 CPU 上完成一次评估、导出和复验，再接入 agent 做树搜索。内置示例使用
随机权重，可以在没有模型账号的环境完成评估。以下命令适用于 macOS/Linux shell。

## 安装方式

需要 Python 3.10+，推荐 3.11 或更新版本。当前 alpha 可用维护者提供的源码包或
wheel 安装。

```sh
python3 -m venv .venv
source .venv/bin/activate
# 在含 pyproject.toml 的源码根目录
python -m pip install '.[cpu]'
autopipeline --version
```

只有 wheel 时，将安装命令换成下面这条，并替换文件路径。

```sh
python -m pip install '/path/to/auto_pipeline-1.4.0a6-py3-none-any.whl[cpu]'
```

| 环境 | 安装方式 |
| --- | --- |
| CPU 评估 | `[cpu]` 安装 PyTorch 和 NumPy。Linux 如需 CPU 专用 PyTorch，可先按 PyTorch 官方说明安装 |
| 已有 PyTorch | 确认 PyTorch 版本满足要求后，可用同一环境安装 wheel 或源码。缺少依赖时按工作负载补齐 |
| 本地 Transformers 编码器 | 安装 `[cpu,models]`，另行准备本地权重 |
| NVIDIA CUDA | 由驱动选择 PyTorch 构建，见[首次服务器使用](#首次服务器使用) |
| 只检查配置 | 安装基础包，使用 `check --no-runtime` |

wheel 包含原生 `pipeline`、搜索引擎和报告。兼容的 AutoKernel、KernelBench 工具
从源码检出运行。PyTorch 计算环境见[官方安装选择器](https://pytorch.org/get-started/locally/)。

## 第一次评估

```sh
autopipeline pipeline init my-pipeline --example residual-mlp --device cpu
autopipeline pipeline check --spec my-pipeline/pipeline.json
autopipeline pipeline evaluate --spec my-pipeline/pipeline.json --run-dir my-pipeline/runs/baseline
autopipeline report my-pipeline/runs/baseline
```

完整 `check` 检查文件及设备，`evaluate` 实际执行数值检查、前向计时、profiler
和最终复验。这些步骤均无需 agent。想看详细结果，打开运行目录内的
`view/dashboard.html` 或 `view/summary.md`。初始候选沿用参考实现，
此时的延迟可作为基线。

## 配置 agent

在运行工作负载的机器安装 Codex CLI，然后登录并检查状态。
[Codex CLI 官方安装说明](https://learn.chatgpt.com/docs/codex/cli)

```sh
codex --version
codex login
codex login status
```

无浏览器服务器可尝试 `codex login --device-auth`，取决于账号是否启用设备码登录。
AutoPipeline 会启动所需的 app-server 子进程。
[官方认证说明](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)

```sh
autopipeline pipeline run --spec my-pipeline/pipeline.json \
  --run-dir my-pipeline/runs/search --executor-backend codex-app-server \
  --max-rounds 2 --beam-width 2 --children-per-expansion 2 \
  --max-agent-calls 4 --max-probe-calls 4
autopipeline report my-pipeline/runs/search
```

首轮最多试两个方向。通过评估的候选可以进入下一轮。当调用预算有限时，已保留的
父分支会先各排一个候选，再排同一父分支的其他候选。实际尝试数取决于首轮结果和
剩余预算。例子只给 executor 配置模型，planner 和 reflector 使用本地策略。
`run` 会使用 agent 服务额度，基线失败会在调用 agent 前停止。

| 参数 | 默认 | 作用 |
| --- | ---: | --- |
| `--beam-width` | 2 | 每轮最多保留的分支数 |
| `--children-per-expansion` | 2 | 每个父分支每轮最多尝试的候选数 |
| `--max-rounds` | 3 | 最多展开的轮数 |
| `--max-agent-calls` | 12 | agent 调用次数上限 |
| `--max-probe-calls` | 12 | 主动探测次数上限 |
| `--command-timeout-seconds` | 600 | 每次 agent 命令的超时 |

做单路对照时把 `--beam-width` 和 `--children-per-expansion` 都设为 1。
对照应使用同一工作负载、模型、推理档位和调用上限，分别记录实际调用、令牌、
总耗时和最终延迟。原生 pipeline 在同一设备上串行评估。延迟接近时应做交错复测。

常用选项见 `autopipeline pipeline run --help`，模型覆盖和附加角色见 `--help-all`。
其他 agent 用 `--executor-command`，接入协议见[开发者入口](dev/README.md#接入其他-coding-agent)。

## 结果与排错

| 终端结果 | 含义 |
| --- | --- |
| 评估通过 | `evaluate` 选中的实现通过最终验证 |
| 找到改进 | 搜索选中有代码改动、评分更好的候选，且最终复验通过。报告收益前仍需重复测量 |
| 完成，未找到改进 | 搜索结束且所选实现通过验证。报告中的候选可能是基线 |
| 运行失败或中断 | 运行目录保留已有记录。处理原因后在新目录重试 |

`result.json` 保留机器可读结果，`promoted_pipeline/` 保存最终选中的代码。
详细目录和日志见[诊断参考](dev/run-artifacts.md)。`report` 的退出码表示报告
能否生成，候选能否使用由运行结果决定。

| 现象 | 处理 |
| --- | --- |
| 找不到命令或 PyTorch | 激活安装时使用的环境，检查 `python` 和 `autopipeline` 来自同一环境 |
| CUDA 不可用 | 核对 CUDA PyTorch、驱动和设备可见性。另试 CPU 时创建新的 `--device cpu` 工作区 |
| Agent 登录、命令或超时错误 | 查看 `nodes/*/agent/` 日志，在同一台机器执行 `codex login status` |
| 数值或 state_dict 检查失败 | 按报告定位输出、权重键或结构差异，修正候选实现 |
| 目录已存在 | 换新名称，或省略 `--run-dir` 自动创建时间戳目录 |
| 权重哈希或导出文件已变化 | 恢复相同权重，或使用当前文件建立新工作负载并重新评估 |

## 导出、复验与重试

```sh
autopipeline pipeline export my-pipeline/runs/search --output-dir my-candidate
autopipeline pipeline evaluate --spec my-candidate/pipeline.json \
  --candidate my-candidate/candidate
```

只执行过评估时，也可以导出 `my-pipeline/runs/baseline`。导出包含候选源码、
参考和验证记录。复验需要相同的外部权重与评测依赖。搜索中断后可以读取部分记录，
并用 `--candidate PREVIOUS/promoted_pipeline` 以已有候选开始新运行。

## 创建与配置工作负载

```sh
autopipeline pipeline examples
autopipeline pipeline init my-model --source /path/to/model-workspace --device cpu
```

| 文件或接口 | 要求 |
| --- | --- |
| `reference.py` | 定义 `Model(torch.nn.Module)` 和 `get_inputs()`。可选 `get_init_inputs()` |
| `candidate.py` | 定义构造参数和 `state_dict` 兼容的 `ModelNew`。缺省时自动生成 |
| `get_inputs()` 与输出 | 输入为位置参数 list/tuple 或关键字参数 dict。输出需包含非空 tensor，支持嵌套容器 |
| `workload/workload.json` | 固定设备、dtype、预热次数、测量次数和容差 |
| `pipeline.json` | 默认只允许改 `candidate.py`。多文件实现提前声明 `editable_paths` |

初始化省略 `--device` 时默认 CUDA。固定权重、输入、dtype 和容差再开始搜索。
原生评测检查多组种子的输出，测量同步 `forward()`，涵盖主机调度和设备同步。
模型加载、输入构造和完整服务链路在计时范围之外。使用可信代码与权重。
完整实现、容差及其他案例见[模型案例](../examples/README.md)。

## 首次服务器使用

在执行机器准备匹配 NVIDIA 驱动的 PyTorch，已有计算环境可以复用。
新环境先按[PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/)选择 CUDA，
再安装 AutoPipeline。下面的预检无需下载权重或登录 agent。

```sh
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())'
autopipeline pipeline init gpu-pipeline --example attention --device cuda
autopipeline pipeline check --spec gpu-pipeline/pipeline.json
autopipeline pipeline evaluate --spec gpu-pipeline/pipeline.json --run-dir gpu-pipeline/runs/baseline
autopipeline report gpu-pipeline/runs/baseline
```

先确认数值通过并有 GPU profiler 记录，再在同一机器连接 agent、替换为目标工作负载。
记录驱动、PyTorch、权重版本、输入形状、dtype 和峰值显存。评估稳定后再开始等预算
搜索对照。兼容工具和额外 CUDA kernel 依赖见[开发者入口](dev/README.md#兼容工具)。
