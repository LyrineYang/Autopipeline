import torch
from torch import nn


class Model(nn.Module):
    """A real composed workload: projection, residual blocks, output head."""
    def __init__(self):
        super().__init__()
        self.projection = nn.Linear(256, 512)
        self.blocks = nn.ModuleList([nn.Sequential(nn.LayerNorm(512), nn.Linear(512, 512), nn.GELU()) for _ in range(3)])
        self.head = nn.Linear(512, 128)

    def forward(self, x):
        x = self.projection(x)
        for block in self.blocks:
            x = x + block(x)
        return self.head(x)


def get_inputs():
    return [torch.randn(8, 64, 256)]


def get_init_inputs():
    return []
