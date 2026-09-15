# 使用案例

案例与运行引擎一起打进 wheel。先运行 `autopipeline pipeline examples` 查看清单，
无需手动寻找源码或下载附件。以下命令在[已安装 AutoPipeline](../docs/quickstart.md#安装方式)的环境执行。
每个案例使用独立目录，重复执行时请换名称；baseline 不调用 agent。
MLP、attention 和自有模型可以先在 CPU 上完成评估与 agent 闭环。

## 1. 组合 MLP：安装和完整流程检查

适用：第一次使用产品、检查 CPU/GPU 环境、熟悉报告和导出。输入为
`[8, 64, 256]` 浮点 tensor；执行投影、三组残差 MLP 和输出层。
使用随机权重，不需要训练或下载；性能结果来自实际执行。

```sh
autopipeline pipeline init workspace/mlp --example residual-mlp --device cpu
autopipeline pipeline evaluate --spec workspace/mlp/pipeline.json --run-dir workspace/mlp/runs/baseline
autopipeline report workspace/mlp/runs/baseline
```

GPU 环境创建另一个 `--device cuda` 工作区。不要把 CPU/GPU 的结果混成一组比较。

## 2. 注意力模块：整个组合实现的优化

适用：测试 QKV 投影、注意力、残差、归一化和 MLP 之间的实现改动。
输入为 `[2, 64, 128]`，模型是无需权重下载的小型 Transformer block。

```sh
autopipeline pipeline init workspace/attention --example attention --device cpu
autopipeline pipeline evaluate --spec workspace/attention/pipeline.json --run-dir workspace/attention/runs/baseline
autopipeline pipeline run --spec workspace/attention/pipeline.json \
  --run-dir workspace/attention/runs/search --executor-backend codex-app-server \
  --max-rounds 1 --children-per-expansion 1 --max-agent-calls 2
```

上述 run 需要[已认证的 agent](../docs/quickstart.md#配置-agent)，会使用服务额度。
GPU 验证时重新创建 `--device cuda` 工作区。候选可以在 `candidate.py` 中重写整个 `forward()`，不局限于一个抽取的 kernel。
第一次验收先确认 agent 确实修改了代码且评测完成；没有提速不能等同于产品失败。
随后再增加预算、输入规模或切换到业务工作负载。

## 3. 本地预训练编码器：已有权重直接接入

适用：已有 BERT 类文本编码器，希望优化 token tensor 到编码结果及 masked mean
pooling 的计算。此案例不包含文本分词、网络、向量数据库或在线服务调度。
它检查与原实现等价，不评估语义检索质量。初始输入是固定形状的随机 token；
业务验证时应由自己的 reference.py 提供代表性 token 输入。

资源准备：安装 `models` extra；wheel 用户见[安装方式](../docs/quickstart.md#安装方式)。模型目录至少包含 `config.json` 与 safetensors
或 PyTorch 权重文件。已有模型可由 Transformers 的 `save_pretrained()` 导出。
没有权重时，可以自行选择模型、确认许可和资源预算后下载。例如下面是可选操作，
不属于 `pipeline init` 的隐式行为：

```sh
# 在源码目录中安装依赖
python -m pip install '.[cpu,models]'
# 可选：固定一个你选定的模型版本；将 REVISION 替换为对应提交号
hf download google-bert/bert-base-uncased --revision REVISION --local-dir /path/to/bert
```

[Hugging Face 下载命令文档](https://huggingface.co/docs/huggingface_hub/guides/cli)
与 [AutoModel 本地加载接口](https://huggingface.co/docs/transformers/model_doc/auto)
解释了资源获取和目录格式。下载量、许可及运行内存由所选模型决定；
这一步不需要为 coding agent 下载另一套权重。

```sh
autopipeline pipeline init workspace/encoder --example hf-local \
  --model-dir /path/to/bert --device cpu
autopipeline pipeline check --spec workspace/encoder/pipeline.json
autopipeline pipeline evaluate --spec workspace/encoder/pipeline.json --run-dir workspace/encoder/runs/baseline
```

初始化记录外部模型文件的 SHA-256。检查和启动运行时校验这些文件，因此大模型
会产生一次额外的磁盘读取；这不计入模型 forward 延迟。运行期间保持资产只读。
`workload/model.json` 设置 batch 和 sequence length，默认 `2 × 32`。
候选只编辑 `candidate.py`，不能改变模型资源、输入形状或参考实现。

导出不会复制大模型：

```sh
autopipeline pipeline export workspace/encoder/runs/baseline --output-dir workspace/encoder-export
```

迁移到其他机器时，把相同权重放到可访问目录，同时修改导出包中
`reference/model.json` 和 `candidate/model.json` 的 `model_dir`，保留相同哈希。
然后重新执行 `pipeline evaluate --spec ... --candidate ... --run-dir ...`。
若换了一套权重，应重新初始化工作负载，不沿用旧结果。

本地自动化测试使用自行初始化并保存的小 BERT checkpoint 验证加载链路，
不下载 Hub 权重。这证明离线加载与计算接口可用，不代表任何预训练模型的业务效果。

## 4. 自己的模型或组合 pipeline

创建 `/path/to/my-workload` 这样的独立目录，将下面代码保存为 `reference.py`。
它是可执行的接口示例；接入业务模型时替换模型与输入生成，不改变接口约定：

```python
# reference.py
import torch
from torch import nn

class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = nn.Linear(128, 256)
        self.head = nn.Linear(256, 16)

    def forward(self, features):
        return self.head(torch.relu(self.encoder(features)))

def get_inputs():
    return [torch.randn(32, 128)]
```

```sh
autopipeline pipeline init workspace/custom --source /path/to/my-workload --device cpu
autopipeline pipeline check --spec workspace/custom/pipeline.json
autopipeline pipeline evaluate --spec workspace/custom/pipeline.json --run-dir workspace/custom/runs/baseline
```

这个接口可以包裹实际模型和多阶段 tensor 计算。固定参考代码负责模型权重加载、
输入和输出语义；优化时保持构造与 state_dict 兼容。模型具有多组形状时，可以
根据评测器设置的随机种子让 `get_inputs()` 选择不同形状，并增加检查种子数量。
不符合 forward 接口的流式任务使用通用命令 evaluator，不要把其服务行为压缩成
一个不等价的 toy forward。

## 扩展到外部 benchmark

实现新的 benchmark 适配器或实验统计，参见[开发者入口](../docs/dev/README.md#外部-benchmark)。
上述模型案例使用产品内置接口，不依赖独立 benchmark 开发线。
