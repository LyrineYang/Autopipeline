"""Packaged workload sources; importing this catalog does not load PyTorch."""

EXAMPLES = {
    "residual-mlp": "Projection → residual MLP blocks → output head; random weights, no download.",
    "attention": "QKV projection → multi-head attention → residual MLP; random weights, no download.",
    "hf-local": "Local pretrained encoder → masked mean pooling; requires a local model directory.",
}
