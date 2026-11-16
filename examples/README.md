# Workload Examples

See the available workloads with `autopipeline pipeline examples`. The built-in examples ship
with the wheel. Each example creates a separate workspace. Choose a new directory when repeating
an example. [Install AutoPipeline](../docs/quickstart.md#installation)

| Example | What it exercises | Assets |
| --- | --- | --- |
| `residual-mlp` | Projection, residual MLP blocks, and an output layer | Random weights |
| `attention` | QKV projection, attention, residuals, normalization, and an MLP | Random weights |
| `hf-local` | A Transformers encoder with masked mean pooling | Existing local weights |
| `--source` | Your PyTorch model or composed pipeline | Your code, inputs, and weights |

## Residual MLP

Use this to verify installation, model execution, and report generation. The input is a floating
point tensor of shape `[8, 64, 256]`.

```sh
autopipeline pipeline init workspace/mlp --example residual-mlp --device cpu
autopipeline pipeline evaluate --spec workspace/mlp/pipeline.json --run-dir workspace/mlp/runs/baseline
autopipeline report workspace/mlp/runs/baseline
```

For GPU measurements, create a new workspace with `--device cuda` and keep its measurements
separate from the CPU baseline.

## Attention

This is a small Transformer block with an input shape of `[2, 64, 128]`. Candidates can rewrite
the full `forward()`, including interactions between the attention and MLP stages.

```sh
autopipeline pipeline init workspace/attention --example attention --device cpu
autopipeline pipeline evaluate --spec workspace/attention/pipeline.json --run-dir workspace/attention/runs/baseline
autopipeline pipeline run --spec workspace/attention/pipeline.json \
  --run-dir workspace/attention/runs/search --executor-backend codex-app-server \
  --max-rounds 2 --beam-width 2 --children-per-expansion 2 \
  --max-agent-calls 4 --max-probe-calls 4
```

The search uses service quota. For a first run, look for a code change, an evaluation record, and
a final verdict. [Connect a coding agent](../docs/quickstart.md#connect-a-coding-agent)

## Local Transformers Encoder

This example measures token tensors through a local encoder and masked mean pooling.
The input uses random tokens of a fixed shape. Use a custom workload for representative
business inputs or retrieval quality checks.

Install the `models` extra and provide a local directory with `config.json` and safetensors or
PyTorch weights. A model saved with Transformers `save_pretrained()` can be used directly.
If you need to fetch weights, choose a model and pin its revision before initializing the
workspace. [Hugging Face download guide](https://huggingface.co/docs/huggingface_hub/guides/cli)

```sh
python -m pip install '.[cpu,models]'
autopipeline pipeline init workspace/encoder --example hf-local \
  --model-dir /path/to/encoder --device cpu
autopipeline pipeline check --spec workspace/encoder/pipeline.json
autopipeline pipeline evaluate --spec workspace/encoder/pipeline.json --run-dir workspace/encoder/runs/baseline
```

Initialization records model asset hashes. Readiness checks verify those hashes.
`workload/model.json` defines batch size and sequence length, with defaults of 2 and 32.
Keep the assets fixed during a run.

```sh
autopipeline pipeline export workspace/encoder/runs/baseline --output-dir workspace/encoder-export
```

On another machine, provide the same weights and update the path in both `reference/model.json`
and `candidate/model.json` in the export. Re-evaluate against the export spec. Start a new
workload for a different set of weights. [Export and recheck](../docs/quickstart.md#export-and-recheck)

## Your Own PyTorch Model

Put the following in `reference.py` in a new source directory. Replace the example module and
input generator with your own implementation when ready.

```python
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

The reference defines input generation, weight loading, and output semantics.
The candidate uses a compatible constructor and `state_dict`. To cover multiple input shapes in
correctness checks, pick shapes in `get_inputs()` based on the seeded random state and configure
enough check seeds. Use a custom evaluator for a streaming or stateful execution contract.
[Developer guide](../docs/dev/README.md#external-benchmarks)
