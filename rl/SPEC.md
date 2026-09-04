# RL 训练契约(Obs / Action / Reward / 权重格式)

C++ 环境(`rl/env/`)与 Python 训练(`rl/training/`)的唯一事实契约。两侧各自实现,以此文件对齐。

## 1. 决策点定义

- 一个"决策点" = 学习方(learner)在一次 `act()` 内选择下一个动作。每个 act 最多 3 个动作,之后或主动 `end` 后进入对方阶段。
- 环境视角:learner 固定为某一方(每个 env 槽位按一半红一半蓝分配),对方(opponent)由对手策略(脚本 .so / 旧权重 / 最新权重)驱动。learner 的观测始终是镜像后的"我在 (0,0) 附近出发"视角,与真实引擎一致。
- `step(a)` 语义:执行 learner 动作 a → 若 act 未结束继续返回同侧观测;否则推进对方阶段与后续回合,直到 learner 的下一个决策点或对局结束。

## 2. 观测(共 OBS_DIM = 8*49 + 20 = 412,float32)

7×7 平面(行主序,y*7+x):

| # | 平面 | 含义 |
|---|---|---|
| 0 | obstacles | 障碍格 1.0 |
| 1 | score_zones | 得分区格 1.0 |
| 2 | my_pos | 我方当前位置 one-hot |
| 3 | opp_belief | 敌方可能位置集合(见 §5 信念更新) |
| 4 | opp_seen_now | 敌方当前被直接看到时的位置 one-hot(否则全 0) |
| 5 | opp_last_known | 敌方最后已知位置 one-hot(无情报则全 0) |
| 6 | my_spawn | 我方出生点 (0,0) 常量 |
| 7 | opp_spawn | 对方出生点 (6,6) 常量(镜像视角) |

标量(20 维,顺序固定):

| # | 含义 | 编码 |
|---|---|---|
| 0-3 | my_facing | one-hot N/E/S/W |
| 4-8 | opp_last_known_facing | one-hot N/E/S/W/未知 |
| 9 | fire_cd | /3 |
| 10 | scan_cd | /3 |
| 11 | my_score | /20 |
| 12 | opp_score | /20 |
| 13 | turn | /24 |
| 14 | actions_used | 本 act 已消耗行动数 /3 |
| 15 | is_blue | 我是蓝方(后手)=1 |
| 16 | opp_visible | 引擎视图中的 opp.visible |
| 17 | opp_directly_visible | T 形直接视野 |
| 18 | intel_age | (当前 turn - 情报获得 turn)/24,无情报=1 |
| 19 | free_turn_available | 复活后免费 TURN 可用 |

## 3. 动作(ACT_DIM = 8)与掩码

| id | 动作 |
|---|---|
| 0 | move |
| 1-4 | turn N/E/S/W |
| 5 | fire |
| 6 | scan |
| 7 | end(主动结束本 act) |

掩码规则(1=可选):
- fire:fire_cd==0;scan:scan_cd==0。
- move:目标格在界内、非障碍、非对方实际占据格。
- turn X:当前朝向 != X。
- actions_used 已达 3:仅 end 可选。
- end 永远可选。

## 4. 奖励(learner 视角)

- 每个 learner 决策点返回:r = W_SCORE * Δ(my_score - opp_score),Δ 为自上一个 learner 决策点以来的变化(包含对方阶段造成的变化,如被杀 -2)。
- 对局结束额外加终局奖励:胜 +1 / 平 0 / 负 -1(加到该局最后一个 learner 决策点的 r 上)。
- W_SCORE 默认 0.1,环境构造可配。
- learner 超时/崩溃:该方判负,终局奖励 -1(崩溃)或按实际比分差+胜负(超时罚分由引擎逻辑处理)。

## 5. 信念平面更新规则(近似,环境侧维护)

- 敌方被直接看到或被 SCAN 到:belief = {实际位置},并记录 intel_turn。
- 我方击杀敌方:belief = {敌方出生点}。
- 否则每过一个敌方行动阶段:belief 向外扩张(曼哈顿距离 ≤3,即可达集),再剔除:障碍格、我方当前 T 形视野内且确认无人的格子、界外格。
- 信念只是输入特征,允许近似;不允许泄漏真实位置。

## 6. 权重文件格式(sdn 二进制)

```
magic:      4 bytes "SDW1"
n_layers:   int32(隐藏层数+1;逐层重复)
per layer:  in_features int32, out_features int32,
            W float32[out*in](行主序,out 行 × in 列), b float32[out]
```

- 网络结构:MLP,hidden 256×2,激活 ReLU;输出层为两组头:policy logits(8)与 value(1)。为部署简单,权重文件只存共享躯干+两个头,按固定顺序:fc1(412→256), fc2(256→256), pi(256→8), vf(256→1),即 n_layers=4。
- Python 侧 `model.py` 的 `save_weights_bin()` 与 C++ 侧 `mlp.h` 的加载必须逐字节兼容。

## 7. 对局与镜像约定

- 环境内一切坐标以 learner 镜像视角呈现;opponent 策略(脚本 .so)拿到的视图由 Match 类按真实引擎规则生成(蓝方自动镜像)。
- 红/蓝 learner 槽位各半;观测含 is_blue 位,策略可按角色分化。
