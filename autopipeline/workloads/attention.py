import torch
from torch import nn


class Model(nn.Module):
    """A small Transformer block with explicit attention for whole-block editing."""
    def __init__(self):
        super().__init__()
        self.qkv = nn.Linear(128, 384)
        self.projection = nn.Linear(128, 128)
        self.norm = nn.LayerNorm(128)
        self.mlp = nn.Sequential(nn.Linear(128, 256), nn.GELU(), nn.Linear(256, 128))

    def forward(self, x):
        batch, sequence, width = x.shape
        q, k, v = self.qkv(x).reshape(batch, sequence, 3, 4, 32).permute(2, 0, 3, 1, 4).unbind(0)
        weights = torch.softmax((q @ k.transpose(-1, -2)) / (32 ** 0.5), dim=-1)
        attended = (weights @ v).transpose(1, 2).reshape(batch, sequence, width)
        x = x + self.projection(attended)
        return x + self.mlp(self.norm(x))


def get_inputs():
    return [torch.randn(2, 64, 128)]
