# model.py - ActorCritic 网络定义 + SDW1 权重导入导出
# 契约见 rl/SPEC.md §2/§3/§6

import struct

import numpy as np
import torch
import torch.nn as nn

OBS_DIM = 8 * 49 + 20  # 412
ACT_DIM = 8
HIDDEN = 256


def _ortho(layer, gain=np.sqrt(2.0)):
    nn.init.orthogonal_(layer.weight, gain)
    nn.init.zeros_(layer.bias)
    return layer


class ActorCritic(nn.Module):
    def __init__(self, obs_dim=OBS_DIM, hidden=HIDDEN, act_dim=ACT_DIM):
        super().__init__()
        self.fc1 = _ortho(nn.Linear(obs_dim, hidden))
        self.fc2 = _ortho(nn.Linear(hidden, hidden))
        self.pi = _ortho(nn.Linear(hidden, act_dim), gain=0.01)
        self.vf = _ortho(nn.Linear(hidden, 1), gain=1.0)

    def forward(self, x):
        h = torch.relu(self.fc1(x))
        h = torch.relu(self.fc2(h))
        return self.pi(h), self.vf(h).squeeze(-1)

    def act(self, obs, mask):
        """obs: (B, OBS_DIM) tensor, mask: (B, ACT_DIM) bool/0-1 tensor.
        返回 action (B,), logprob (B,), value (B,)。"""
        logits, value = self.forward(obs)
        logits = logits.masked_fill(mask <= 0, -1e9)
        dist = torch.distributions.Categorical(logits=logits)
        action = dist.sample()
        return action, dist.log_prob(action), value

    def evaluate(self, obs, mask, action):
        logits, value = self.forward(obs)
        logits = logits.masked_fill(mask <= 0, -1e9)
        dist = torch.distributions.Categorical(logits=logits)
        return dist.log_prob(action), dist.entropy(), value

    # ---- SDW1 二进制权重(与 C++ mlp.h 逐字节兼容)----
    def save_weights_bin(self, path):
        layers = [self.fc1, self.fc2, self.pi, self.vf]
        with open(path, "wb") as f:
            f.write(b"SDW1")
            f.write(struct.pack("<i", len(layers)))
            for layer in layers:
                w = layer.weight.detach().cpu().numpy().astype(np.float32)  # (out, in)
                b = layer.bias.detach().cpu().numpy().astype(np.float32)
                out_f, in_f = w.shape
                f.write(struct.pack("<ii", in_f, out_f))
                f.write(w.tobytes())  # 行主序 out×in
                f.write(b.tobytes())

    def load_weights_bin(self, path):
        with open(path, "rb") as f:
            data = f.read()
        assert data[:4] == b"SDW1", "bad magic"
        n_layers = struct.unpack_from("<i", data, 4)[0]
        assert n_layers == 4, f"expect 4 layers, got {n_layers}"
        off = 8
        for layer in [self.fc1, self.fc2, self.pi, self.vf]:
            in_f, out_f = struct.unpack_from("<ii", data, off)
            off += 8
            w = np.frombuffer(data, dtype=np.float32, count=out_f * in_f, offset=off)
            off += 4 * out_f * in_f
            b = np.frombuffer(data, dtype=np.float32, count=out_f, offset=off)
            off += 4 * out_f
            layer.weight.data.copy_(torch.from_numpy(w.reshape(out_f, in_f).copy()))
            layer.bias.data.copy_(torch.from_numpy(b.copy()))
        return self
