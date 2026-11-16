# 使用指南

原生 `autopipeline pipeline` 和 `autopipeline report` 安装后可以在任意目录使用。
旧 AutoKernel 工具需要源码检出。本页命令采用 macOS/Linux shell；第一次使用建议
先完成 [README 的 CPU 流程](../README.md#快速开始)，再接入 agent 或 GPU。

## 安装方式

Python 3.10+；使用独立环境。发布准备阶段，从维护者取得对应版本的源码包或
wheel，不要将 PyPI 上的同名包当作本产品的发布版本。

**源码安装**：解压源码包并进入含 pyproject.toml 的目录，然后执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[cpu]'
autopipeline --version
autopipeline pipeline examples
```

**wheel 安装**：将下面路径替换为取得的实际文件路径。`[cpu]` 安装计算依赖；
如果已有匹配的 PyTorch 环境，可去掉 extra，仅安装基础包。

```sh
python -m pip install '/path/to/auto_pipeline-1.4.0a5-py3-none-any.whl[cpu]'
autopipeline --version
```

| 需求 | 从源码安装时选择 | 说明 |
| --- | --- | --- |
| 配置、文件检查，无模型运行 | `python -m pip install .` | 用 `check --no-runtime`；不代表执行成功 |
| CPU 模型评估 | `python -m pip install '.[cpu]'` | PyTorch 与 NumPy |
| 本地 Transformers 模型 | `python -m pip install '.[cpu,models]'` | 加载依赖不包含模型权重 |
| NVIDIA Linux 环境及旧 kernel 工具 | `python -m pip install '.[gpu]'` | 先确认 CUDA PyTorch 与驱动兼容 |

依赖安装可能联网。已经由服务器管理的 PyTorch 环境，安装基础包并按工作负载补齐
依赖即可。不要为了基础 CLI 重新替换计算栈。wheel 同样支持 `[cpu,models]` 等 extras。
当前顶层包名仍含 tree、pipeline 等，推荐独立环境，避免与其他项目的同名包混用。

## 创建与配置工作负载

```sh
autopipeline pipeline init my-pipeline --example residual-mlp --device cpu
```

默认案例是 residual-mlp；**省略 `--device` 时默认 CUDA**。设备不会自动回退。
`attention` 和 `hf-local` 的场景、输入与资源要求见[案例](../examples/README.md)。

创建后的结构：

```text
my-pipeline/
├── pipeline.json          # 可编辑范围、评测命令、数值容差等契约
├── README.md
└── workload/
    ├── reference.py       # 固定模型与输入生成
    ├── candidate.py       # 默认允许 agent 修改的实现
    └── workload.json      # 设备、dtype、预热、计时与检查配置
```

项目内的 README 可独立使用：先给出评估、报告、导出与复验流程，再介绍 agent。
终端每一步都会打印下一步命令，包含可复制的实际路径。

| 配置位置 | 内容与默认值 |
| --- | --- |
| `workload/workload.json` | dtype=float32，warmup=5，repeats=20，check_seeds=3，atol=1e-5，rtol=1e-4 |
| `pipeline.json` 的 editable_paths | 默认只有 candidate.py；多文件实现需在开始前声明 |
| `pipeline.json` 的 metadata.numerical_tolerances | 数值容差；调整时与 workload.json 保持一致 |
| hf-local 的 `workload/model.json` | 本地权重目录、哈希、batch 和 sequence length |

在运行前固定配置。改变设备、输入、权重、dtype 或容差意味着一次新的比较，
不能把不同条件的结果合并成同一加速比。将环境、缓存和无关文件放在 workload 外。
参考代码、输入生成和评测器不属于 agent 的默认编辑范围。

### 使用自己的代码

```sh
autopipeline pipeline init my-model --source /path/to/model-workspace --device cpu
```

| 文件/接口 | 要求 |
| --- | --- |
| reference.py | 提供 Model(torch.nn.Module)、get_inputs()；可选 get_init_inputs() |
| candidate.py | 提供 ModelNew，构造参数与 state_dict 键兼容；未提供时自动生成 |
| get_inputs() | 返回位置参数 list/tuple，或关键字参数 dict；支持嵌套 tensor |
| forward() 输出 | tensor 或嵌套 list/tuple/dict；至少含一个非空 tensor |
| 其他本地文件 | 作为固定文件复制；工作区不支持符号链接 |

所有输出 tensor 都会检查。整数要求精确相等；浮点逐元素满足
`abs(candidate-reference) <= atol + rtol*abs(reference)`。
非有限数、结构/dtype 不一致和不兼容的 state_dict 会失败。
复杂数 tensor、自定义流式/有状态协议需要其他 evaluator。

大模型资产使用固定外部路径；hf-local 自动记录其哈希。自定义模型的外部资产由
使用者保持版本一致。候选按本地代码执行；工作区复制用于隔离文件修改，
不是操作系统安全沙箱。完整自定义模型代码见[案例](../examples/README.md#4-自己的模型或组合-pipeline)。

## 先评估，再调用 agent

```sh
autopipeline pipeline check --spec my-pipeline/pipeline.json --no-runtime
autopipeline pipeline check --spec my-pipeline/pipeline.json
autopipeline pipeline evaluate --spec my-pipeline/pipeline.json --run-dir my-pipeline/runs/baseline
```

`--no-runtime` 只检查配置和文件。完整 check 会检查 PyTorch 和所选设备，
不会联系 agent 服务，也不会代替实际模型执行或证明登录有效。

evaluate 执行参考与候选，跨多个种子检查输出，预热后测量同步 forward 的
中位延迟，再单独采集 profiler。模型加载和输入构造不计入延迟。
相同初始实现的重复测量不要求“自己击败自己”；修改后的候选需要通过数值与性能
判定，最终选中代码还会重新评估。profiler 不可用时会记录原因。

## 配置 agent

CPU 与 CUDA 使用同样的 agent 接口。先在执行机器安装
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)，然后完成登录：

```sh
codex --version
codex login
codex login status
```

无浏览器服务器可按[官方认证说明](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
使用 `codex login --device-auth`，是否可用取决于账号或工作区设置。
AutoPipeline 启动 app-server 子进程，不要求用户手工启动一个长期运行的服务。
登录应在实际执行优化的机器上完成，不在项目中保存或传递登录令牌。

第一次运行只给一个候选机会：

```sh
autopipeline pipeline run --spec my-pipeline/pipeline.json \
  --run-dir my-pipeline/runs/search \
  --executor-backend codex-app-server \
  --max-rounds 1 --children-per-expansion 1 --beam-width 1 \
  --max-agent-calls 2 --max-probe-calls 2
autopipeline report my-pipeline/runs/search
```

只有 run 会使用 agent 服务额度；基线失败会阻止后续 agent 调用。
上述配置只启用模型 executor；planner/reflector 使用本地策略。如需模型角色，
增加 `--planner-backend codex-app-server` 或 `--reflector-backend codex-app-server`，
并相应预留调用预算。默认模型不可用时，用 `--executor-model` 指定当前账号可用的
模型。需要控制推理档位时使用 `--executor-reasoning-effort`。

| 控制项 | 作用 |
| --- | --- |
| max-rounds | 展开轮数上限 |
| children-per-expansion | 每个候选在一轮中尝试的方向数上限 |
| beam-width | 一轮结束后保留并允许继续细化的候选数上限 |
| max-agent-calls | agent 调用次数上限，不是金额上限；也不保证全部预算都会用完 |
| max-probe-calls | 主动探测次数上限；原生 pipeline run 当前要求正数 |
| command-timeout-seconds | 默认 600 秒，限制单次 agent 命令 |

原生 PyTorch pipeline 串行评估候选。把 `--children-per-expansion` 和
`--beam-width` 都设为 1，可逐轮沿一个候选迭代。两者设为 2 可尝试不同方向
并保留最多两个候选。比较两种配置时，使用同一工作负载、模型、推理档位和
调用上限，并使用不同的运行目录。用量报告提供实际令牌数，调用预算按次数计。

`autopipeline pipeline run --help` 展示常用连接参数、搜索规模、调用上限与超时；
`autopipeline pipeline run --help-all` 展示完整参数。已有高级参数仍可直接使用，
不需要另一个执行模式。省略 `--run-dir` 会在 spec 旁的 `runs/` 下创建带时间戳的
目录，运行前打印设备、可编辑文件、预算和实际目录。

其他 coding agent 可通过命令接入；协议实现见[开发者入口](dev/README.md#接入其他-coding-agent)。
正常使用内置后端无需了解内部消息格式。

## 结果与排错

先看终端的结果解释，或执行 `autopipeline report RUN_DIR`。终端、HTML 和 Markdown
报告优先说明评估通过、找到改进、未找到改进、失败或中断，再提供过程指标。
result.json 保留机器可读状态；不要仅以退出码判断是否提速。

| 状态 | 用户应如何理解 |
| --- | --- |
| verified | evaluate 的最终评估通过，可能只是初始基线 |
| completed_improved | search 选择了有实现改动、搜索分数更好的候选；仍需查看最终复验并重复测量 |
| completed_no_improvement | 已完成搜索并验证所选实现，但没有选出更好的候选 |
| failed / interrupted | 失败或中断；先读失败原因，不能据此宣布候选可用 |

完成的评估/搜索退出 0；运行失败通常退出 1，配置/路径错误退出 2，中断退出 130。
全部 agent 尝试失败不算成功优化。搜索评分与最终计时可能受测量波动影响，
发布加速比前应在相同输入、权重和空闲设备上重复比较。

| 路径 | 用途 |
| --- | --- |
| result.json | 最终状态、候选、文件哈希与复验结论 |
| promoted_pipeline/ | 最终选中代码，也可能是未改变的基线 |
| view/dashboard.html、view/summary.md | 浏览器报告与文本摘要 |

详细运行记录与日志位置见[开发者诊断参考](dev/run-artifacts.md)。运行在很早阶段失败，
只要留下 result.json，report 也能解释结果。`report` 退出 0 表示报告已生成，
不代表运行成功；显示“尚未完成”也不保证进程仍在运行。

常见故障：

| 现象 | 下一步 |
| --- | --- |
| 找不到 autopipeline 或 torch | 激活安装时使用的环境，检查 python 和 autopipeline 是否来自同一环境 |
| CUDA 不可用 | 检查 CUDA PyTorch 与设备可见性；需要 CPU 时重新 init --device cpu |
| 目录已存在 | 项目和导出换新名称；评估/搜索可省略 --run-dir 自动创建新目录 |
| No executor configured / codex is not on PATH | 配置 executor，并在执行机器安装 Codex；单纯评估使用 evaluate |
| agent 认证失败或超时 | 检查 nodes/*/agent 日志、同机登录和网络；确认原因后再重试或调整超时 |
| 数值、结构或 state_dict 失败 | 对比候选与固定参考；修正实现，不让 agent 放宽验收容差 |
| 模型资产哈希改变 | 恢复原权重；若有意更换模型，重新初始化，不复用旧比较 |
| 导出提示文件已改变 | 在新目录重新评估后再导出，不修改验证收据 |
| report 找不到运行记录 | 传入执行时打印的运行目录，而不是项目根目录或导出目录 |

向维护者提供版本、命令、设备环境、result.json 与相关脱敏日志；不要上传
访问令牌、私有权重或含敏感输入的 trace。安全问题按[安全说明](../SECURITY.md)处理。

## 导出、复验与重试

```sh
autopipeline pipeline export my-pipeline/runs/search --output-dir my-candidate
autopipeline pipeline evaluate --spec my-candidate/pipeline.json \
  --candidate my-candidate/candidate --run-dir my-candidate/runs/recheck
```

导出保存候选源码、固定参考与验证记录，不会自动部署服务。
集成时从候选目录使用 ModelNew，并保持同样的构造、权重和输入。
外部模型资产及自定义 evaluator 依赖必须在目标机器可用；hf-local 的路径迁移见
[模型案例](../examples/README.md#3-本地预训练编码器已有权重直接接入)。

运行目录从不复用。中断留下的产物可以检查，但目前没有完整调度状态的恢复功能。
重新运行时使用新 run-dir；可通过 `--candidate PREVIOUS/promoted_pipeline`
指定已经存在的候选工作区作为起点，同时保留相同参考 spec。

## 首次服务器使用

按下面顺序排除问题，准备一张能容纳小案例的 NVIDIA GPU 即可：

1. **安装与设备**：准备 Python、CUDA PyTorch 和可见 GPU。仓库的 uv 配置在 Linux
   上选择 CUDA 12.8 wheel，`uv sync --extra gpu` 使用该配置；已有计算环境按需安装库。
2. **纯模型执行**：新建 attention CUDA 工作区，执行 check 和 evaluate；先确认数值
   与 profiler，再涉及 agent 账号。
3. **真实 agent 闭环**：按前面的单轮预算运行，检查实际代码修改、评估和最终选择。
4. **业务收益**：固定一项代表性模型、权重、输入及预算，重复测量后再决定扩大实验。

```sh
autopipeline pipeline init gpu-pipeline --example attention --device cuda
autopipeline pipeline check --spec gpu-pipeline/pipeline.json
autopipeline pipeline evaluate --spec gpu-pipeline/pipeline.json --run-dir gpu-pipeline/runs/baseline
```

准备账号仅在调用 agent 时需要；大模型权重、多 GPU 和完整业务数据不是首次
环境检查的前提。没有服务器时，第 3 步也可以在 CPU 上先做，回答“agent 能否完成
编辑与验证”；它不能回答 CUDA 兼容性或 GPU 加速收益。

## Original AutoKernel path

原有单 kernel 工具仍受支持，入口已集中到[开发与兼容工具指南](dev/README.md#兼容工具)。
普通 pipeline 使用不需要先执行 profile/extract。
