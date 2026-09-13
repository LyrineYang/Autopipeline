"""Offline pretrained encoder workload. Asset acquisition is a separate action."""
import json
from pathlib import Path

import torch
from torch import nn
from transformers import AutoConfig, AutoModel


def settings():
    return json.loads(Path(__file__).with_name("model.json").read_text())


class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = AutoModel.from_pretrained(
            settings()["model_dir"], local_files_only=True, trust_remote_code=False,
        )

    def forward(self, input_ids, attention_mask):
        hidden = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
        return {"embedding": (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1)}


def get_inputs():
    config = settings()
    model = AutoConfig.from_pretrained(config["model_dir"], local_files_only=True, trust_remote_code=False)
    shape = (config["batch_size"], config["sequence_length"])
    tokens = torch.randint(0, model.vocab_size, shape)
    mask = torch.ones(shape, dtype=torch.long)
    # Exercise masking as well as full-length input; no tokenizer/network call.
    mask[0, shape[1] // 2:] = 0
    return {"input_ids": tokens, "attention_mask": mask}
