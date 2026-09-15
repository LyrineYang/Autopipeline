# AutoPipeline

**让 coding agent 迭代优化 PyTorch 实现，用固定评测选择候选，交付可重新验证的代码。**

AutoPipeline 面向模型推理与组合 pipeline 的计算优化。你提供参考实现、输入、
目标设备和调用预算；agent 修改候选代码，工具执行数值校验、计时和 profiler，
再决定保留或拒绝。最终得到候选源码、运行报告和可独立复验的导出包。

基于 [AutoKernel](https://github.com/RightNow-AI/autokernel) 扩展，既能优化整个
模型实现，也保留原有单 kernel 工作流。**当前为 alpha 产品**：CPU 安装和评估
流程已验证；真实 agent 的优化表现、目标 CUDA 环境与加速收益仍需实测。

[开始使用](#快速开始) · [使用指南](docs/quickstart.md) · [模型案例](examples/README.md) ·
[结果与排错](docs/quickstart.md#结果与排错)

## 你可以用它做什么

| 目标 | 输入 | 推荐入口 |
| --- | --- | --- |
| 跑通安装、评估和导出 | Python、PyTorch，无需权重或 agent 账号 | 内置 `residual-mlp` |
| 优化注意力与多算子组合 | 固定形状、dtype 和设备 | 内置 `attention` |
| 优化已有模型的计算实现 | 自己的模型代码、权重和代表性输入 | `init --source` |
| 优化本地编码器的前向与池化 | 本地 Transformers 编码器目录 | `hf-local` |

内置 evaluator 测量模型 `forward()` 的延迟，包括主机调度和设备同步，
不包含模型加载、输入构造、分词、网络或完整线上服务延迟。流式协议及其他
执行方式可接自定义 evaluator，不能把 forward 结果当作线上服务的整体收益。

## 工作方式

```text
参考实现 + 输入 + 设备与预算
              ↓
     数值检查、基线计时、profiler
              ↓
候选修改 → 评估 → 保留或拒绝 → 在预算内继续
              ↓
     最终复验 → 报告 → 导出源码
```

- **固定比较条件**：默认仅允许修改 `candidate.py`；参考、输入与评测配置固定。
- **实际执行**：多组种子检查输出，记录延迟样本与算子 profiler。
- **限制成本**：配置轮数、分支数、调用数及超时；当前原生 pipeline 串行测量。
- **保留证据**：运行参考快照、候选、失败日志和最终复验结果留在各自运行目录。

优化不保证提速。未找到更好实现时保留已验证的基线；报告会区别“评估通过”、
“找到改进”和“未找到改进”，不会把成功退出等同于加速。

## 快速开始

要求 Python 3.10+，建议在独立虚拟环境使用。**发布准备阶段请使用维护者提供的、
包含本页的源码包或 wheel；尚未提供 PyPI 安装路径。** 以下命令从源码根目录开始，
安装依赖可能联网，但案例本身不下载权重、不调用 agent 服务。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[cpu]'
autopipeline --version

autopipeline pipeline init workspace/demo --example residual-mlp --device cpu
autopipeline pipeline check --spec workspace/demo/pipeline.json
autopipeline pipeline evaluate --spec workspace/demo/pipeline.json --run-dir workspace/demo/runs/baseline
autopipeline report workspace/demo/runs/baseline
```

看到 `Status: verified` 表示这次候选通过最终评估。打开
`workspace/demo/runs/baseline/view/dashboard.html` 查看报告，或直接阅读同目录的
`summary.md`。初始候选与参考相同，因此这里验证的是产品流程，不是优化收益。

继续导出并重新验证：

```sh
autopipeline pipeline export workspace/demo/runs/baseline --output-dir workspace/demo-export
autopipeline pipeline evaluate --spec workspace/demo-export/pipeline.json \
  --candidate workspace/demo-export/candidate --run-dir workspace/demo-export/runs/recheck
```

目录已存在时请换新名称，工具不会覆盖旧结果。只有 wheel、已有 PyTorch 或需要
纯配置检查时，参见[安装方式](docs/quickstart.md#安装方式)。

## 让 agent 开始优化

**CPU 也可以先验证真实 agent 闭环，无需等待 GPU 服务器。** 在执行机器上按
[Codex CLI 官方说明](https://learn.chatgpt.com/docs/codex/cli)安装，并完成
[登录](https://learn.chatgpt.com/docs/auth)。然后接着上面的 CPU 工作负载运行：

```sh
autopipeline pipeline run --spec workspace/demo/pipeline.json \
  --run-dir workspace/demo/runs/search \
  --executor-backend codex-app-server \
  --max-rounds 1 --children-per-expansion 1 --beam-width 1 \
  --max-agent-calls 2 --max-probe-calls 2
autopipeline report workspace/demo/runs/search
```

`init`、`check`、`evaluate` 不调用模型服务；`run` 会使用已配置的 agent 额度。
调用次数限制不是金额上限。以上只给 executor 配置模型，planner/reflector
使用本地策略。其他 agent 通过 `--executor-command` 接入，见[使用指南](docs/quickstart.md#配置-agent)。

在 NVIDIA 服务器上，先准备兼容驱动的 CUDA PyTorch，创建一个新的
`--device cuda` 工作负载，再执行相同的 check → evaluate → run 流程。
设备不可用会明确失败，不会自动切回 CPU。详见[首次服务器使用](docs/quickstart.md#首次服务器使用)。

## 换成你的模型

```sh
# 查看所有内置案例及资源要求
autopipeline pipeline examples

# 注意力 → 残差 → MLP：随机初始化，无需权重下载
autopipeline pipeline init workspace/attention --example attention --device cpu

# 接入你自己的实现
autopipeline pipeline init workspace/custom --source /path/to/my-workload --device cpu
```

自有源码目录至少包含 `reference.py`，提供 `Model`、`get_inputs()` 和可选的
`get_init_inputs()`；候选提供构造参数和 state_dict 兼容的 `ModelNew`。
未提供 `candidate.py` 时会生成继承参考模型的初始实现。完整可复制的模型例子见
[自有模型案例](examples/README.md#4-自己的模型或组合-pipeline)。

“被优化的模型”与“修改代码的 agent”分别准备：

| 资源 | 是否需要下载 |
| --- | --- |
| 内置 MLP / attention | 不需要；随机权重即可执行计算 |
| 自己的模型或预训练工作负载 | 使用已有本地代码和权重；缺少权重时另行获取 |
| Codex 等 coding agent | 连接其 CLI/服务，不要求本地下载 agent 权重 |

`hf-local` 加载已有本地编码器，不联网补齐权重，也不执行 Hub 自定义代码。
模型资产哈希用于固定比较版本；导出不复制大模型，迁移时需要相同权重。
具体准备、输入范围和迁移步骤见[本地编码器案例](examples/README.md#3-本地预训练编码器已有权重直接接入)。

## 使用边界

- 这是开发工具；候选代码在调用者环境中执行，复制工作区不等于操作系统沙箱。
- 导出用于代码集成，不会自动部署推理服务；目前不支持完整搜索调度状态的断点恢复。
- CPU 案例不证明 CUDA 兼容性、预训练模型业务质量或真实 agent 的优化收益。
## 帮助与反馈

[使用指南](docs/quickstart.md) · [案例](examples/README.md) · [文档导航](docs/index.md)

问题反馈请附版本、执行命令、设备/依赖环境和脱敏后的失败日志；安全问题按
[安全说明](SECURITY.md)私下报告。

扩展 agent、接入 benchmark 或参与源码开发，请进入[开发者入口](docs/dev/README.md)。

采用 [MIT 许可](LICENSE)，保留 AutoKernel 的来源与版权信息。
