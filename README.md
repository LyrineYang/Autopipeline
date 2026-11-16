# AutoPipeline

**用树搜索和 coding agent 优化 PyTorch 工作负载，交付可复验的代码。**

你提供参考实现、输入、设备和调用预算。AutoPipeline 让 agent 尝试不同实现，
独立检查输出并测量延迟，再保留通过评估的分支继续迭代。每次运行都会留下
候选代码、评测记录和报告。通过最终验证的候选可以导出，在另一环境重新评估。

基于 [AutoKernel](https://github.com/RightNow-AI/autokernel) 扩展。
当前为 alpha，已在 CPU 上验证安装、评估、导出和真实 agent 闭环。实际加速比
取决于工作负载和设备，CUDA 兼容性与多分支收益需在目标机器上验证。

[完整 Quickstart](docs/quickstart.md) · [模型案例](examples/README.md) ·
[CUDA 环境](docs/quickstart.md#首次服务器使用) · [开发者入口](docs/dev/README.md)

## 快速开始

使用 Python 3.10+，推荐 3.11 或更新版本。取得 AutoPipeline 源码包后，
在源码根目录执行下面的命令。内置 MLP 使用随机权重，评估无需 agent 账号。
如果只有 wheel，参见[安装方式](docs/quickstart.md#安装方式)。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[cpu]'
autopipeline --version

autopipeline pipeline init workspace/demo --device cpu
autopipeline pipeline check --spec workspace/demo/pipeline.json
autopipeline pipeline evaluate --spec workspace/demo/pipeline.json --run-dir workspace/demo/runs/baseline
autopipeline report workspace/demo/runs/baseline
```

打开 `workspace/demo/runs/baseline/view/dashboard.html` 查看输出校验、前向延迟和
profiler。初始候选沿用参考实现，基线评估通过表示这条执行路径可用。

## 用树搜索优化

在同一台机器上安装并登录 Codex CLI，确认 `codex login status` 成功。
接着运行上面的工作负载。也可以用 `--executor-command` 接入自己的 agent。
安装和登录详见[配置 agent](docs/quickstart.md#配置-agent)。

```sh
autopipeline pipeline run --spec workspace/demo/pipeline.json \
  --run-dir workspace/demo/runs/search --executor-backend codex-app-server \
  --max-rounds 2 --beam-width 2 --children-per-expansion 2 \
  --max-agent-calls 4 --max-probe-calls 4
autopipeline report workspace/demo/runs/search
```

这组参数允许首轮尝试两个方向，后续从通过评估的分支继续，最多调用 agent 四次。
如果没有候选带来通过复验的改进，报告会给出本次运行的实际结果。
调用次数限制控制使用次数，服务费用取决于所用账号和模型。

## 导出和复验

```sh
autopipeline pipeline export workspace/demo/runs/search --output-dir workspace/demo-export
autopipeline pipeline evaluate --spec workspace/demo-export/pipeline.json \
  --candidate workspace/demo-export/candidate
```

导出要求运行通过最终验证。只完成 CPU 基线时，也可以把上面的
`workspace/demo/runs/search` 换成 `workspace/demo/runs/baseline`。
外部权重需在复验机器单独提供。旧结果目录会保留，重跑时省略 `--run-dir`
即可创建新目录。

## 换成自己的负载

| 负载 | 如何开始 |
| --- | --- |
| Attention 与残差 MLP | `autopipeline pipeline init work --example attention --device cpu` |
| 本地 Transformers 编码器 | `autopipeline pipeline init work --example hf-local --model-dir /path/to/encoder --device cpu` |
| 自有 PyTorch 模型或组合 pipeline | `autopipeline pipeline init work --source /path/to/workload --device cpu` |

自有负载提供 `reference.py`、`Model` 和 `get_inputs()`。默认只允许 agent
修改 `candidate.py`。原生评测测量同步的 `forward()`，其他执行协议可接
自定义评测器。接入完整说明和服务器预检见[使用指南](docs/quickstart.md)。

仅运行来自可信来源的工作负载。候选代码在本地执行，工作区复制只隔离文件修改。
原有单算子和 KernelBench 命令可从源码检出使用，参见[兼容工具](docs/dev/README.md#兼容工具)。

## 反馈与许可

反馈问题时附版本、执行命令、设备环境和脱敏后的运行记录。
安全问题按[安全说明](SECURITY.md)私下报告。

采用 [MIT 许可](LICENSE)，保留 AutoKernel 的来源与版权信息。
