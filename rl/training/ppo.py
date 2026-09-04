# ppo.py - 标准 clipped PPO(GAE),输入为收集好的 rollout 批次
# 与具体环境解耦:批次由 rl/env 的 C++ VecEnv 收集为 numpy 数组。

import numpy as np
import torch

from model import ActorCritic, OBS_DIM, ACT_DIM


class PPO:
    def __init__(self, obs_dim=OBS_DIM, act_dim=ACT_DIM, lr=3e-4, clip=0.2,
                 epochs=4, minibatch=4096, ent_coef=0.01, vf_coef=0.5,
                 gamma=0.99, gae_lambda=0.95, max_grad_norm=0.5, device="cpu"):
        self.device = device
        self.net = ActorCritic(obs_dim, act_dim=act_dim).to(device)
        self.opt = torch.optim.Adam(self.net.parameters(), lr=lr)
        self.clip = clip
        self.epochs = epochs
        self.minibatch = minibatch
        self.ent_coef = ent_coef
        self.vf_coef = vf_coef
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.max_grad_norm = max_grad_norm
        self.lr = lr

    def set_lr(self, lr):
        self.lr = lr
        for g in self.opt.param_groups:
            g["lr"] = lr

    @staticmethod
    def compute_gae(rewards, values, dones, last_value, gamma, lam):
        """rewards/values/dones: (T,) numpy。last_value: 终止状态的 bootstrap value
        (若最后一个 transition 是真实结束则为 0)。返回 adv, ret。"""
        T = len(rewards)
        adv = np.zeros(T, dtype=np.float32)
        last_gae = 0.0
        for t in reversed(range(T)):
            next_value = last_value if t == T - 1 else values[t + 1]
            non_terminal = 1.0 - dones[t]
            delta = rewards[t] + gamma * next_value * non_terminal - values[t]
            last_gae = delta + gamma * lam * non_terminal * last_gae
            adv[t] = last_gae
        return adv, adv + values

    def update(self, batch):
        """batch: dict of numpy arrays, 键:
        obs (N,OBS_DIM), mask (N,ACT_DIM), action (N,), logprob (N,),
        adv (N,), ret (N,). adv 需已在外面按各 episode 算好 GAE。
        返回统计 dict。"""
        obs = torch.as_tensor(batch["obs"], dtype=torch.float32, device=self.device)
        mask = torch.as_tensor(batch["mask"], dtype=torch.float32, device=self.device)
        action = torch.as_tensor(batch["action"], dtype=torch.long, device=self.device)
        old_logprob = torch.as_tensor(batch["logprob"], dtype=torch.float32, device=self.device)
        adv = torch.as_tensor(batch["adv"], dtype=torch.float32, device=self.device)
        ret = torch.as_tensor(batch["ret"], dtype=torch.float32, device=self.device)

        adv = (adv - adv.mean()) / (adv.std() + 1e-8)

        N = len(obs)
        idx = np.arange(N)
        stats = {"pg": 0.0, "vf": 0.0, "ent": 0.0, "clipfrac": 0.0, "approx_kl": 0.0, "n": 0}
        for _ in range(self.epochs):
            np.random.shuffle(idx)
            for start in range(0, N, self.minibatch):
                mb = idx[start:start + self.minibatch]
                mb_t = torch.as_tensor(mb, device=self.device)
                logprob, entropy, value = self.net.evaluate(obs[mb_t], mask[mb_t], action[mb_t])
                ratio = torch.exp(logprob - old_logprob[mb_t])
                pg1 = -adv[mb_t] * ratio
                pg2 = -adv[mb_t] * torch.clamp(ratio, 1 - self.clip, 1 + self.clip)
                pg_loss = torch.max(pg1, pg2).mean()
                vf_loss = 0.5 * (value - ret[mb_t]).pow(2).mean()
                ent = entropy.mean()
                loss = pg_loss + self.vf_coef * vf_loss - self.ent_coef * ent

                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.net.parameters(), self.max_grad_norm)
                self.opt.step()

                with torch.no_grad():
                    approx_kl = (old_logprob[mb_t] - logprob).mean().item()
                    clipfrac = ((ratio - 1).abs() > self.clip).float().mean().item()
                stats["pg"] += pg_loss.item(); stats["vf"] += vf_loss.item()
                stats["ent"] += ent.item(); stats["clipfrac"] += clipfrac
                stats["approx_kl"] += approx_kl; stats["n"] += 1
        for k in ("pg", "vf", "ent", "clipfrac", "approx_kl"):
            stats[k] /= max(stats["n"], 1)
        return stats
