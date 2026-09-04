# 强化学习训练基础设施

为哨兵大战训练 RL AI 的完整管线。契约见 `SPEC.md`(**改任何一侧前先读它**)。

## 结构

```
rl/
├── SPEC.md              # 观测(412)/动作(8)/奖励/权重格式契约
├── env/                 # C++ 环境(链接引擎 Match 类)
│   ├── mlp.h            # SDW1 权重加载 + MLP 前向
│   ├── obs_builder.h    # 观测构建 + 信念追踪(训练/部署共用)
│   ├── episode.h        # 轨迹缓冲 + GAE
│   ├── sentry_env.cpp   # pybind11 模块(多线程 collect/eval)
│   ├── build.sh         # 构建 sentry_env
│   └── test_*.cpp       # 组件单测
├── training/
│   ├── model.py         # ActorCritic + SDW1 导入导出
│   ├── ppo.py           # PPO(clip + GAE)
│   ├── train.py         # 主训练循环(smoke / league)
│   ├── eval_real.py     # 真引擎验收评测
│   ├── export_cpp.py    # sdw → ai/rl_weights.h
│   └── monitor.py       # 训练日志摘要
├── weights/  checkpoints/  logs/  runs/   # 产物(git 忽略)
```

## 流程

```bash
# 1. 构建引擎(含 Match)与环境
cd engine/build && cmake .. && make && cd ../../rl/env && ./build.sh

# 2. 冒烟:只对脚本池,确认学习曲线
cd ../training && python3 train.py --phase smoke --max-iters 50

# 3. 正式:league 自我对弈
python3 train.py --phase league --n-threads 32 --steps-per-iter 131072

# 4. 监控
python3 monitor.py --tail 20

# 5. 导出 + 编译部署 AI
python3 export_cpp.py --weights ../weights/final.sdw --out ../../ai/rl_weights.h
cd ../../ai && bash build_rl_ai.sh

# 6. 真引擎终评(验收)
cd ../rl/training && python3 eval_real.py --ai ../../ai/rl_ai.so --games 2000 --jobs 32
```

## 设计要点

- **训练/部署同一份观测代码**(`obs_builder.h`):只使用选手可得信息(Board 视图 + 行动返回值),杜绝信息泄漏与 train/serve 偏差。
- **部署 = masked argmax + 手写 MLP 前向 + 异常回退安全壳**(`ai/rl_ai.cpp`),权重内嵌 `rl_weights.h`(由 export_cpp.py 生成),无外部依赖。
- **league 对手池**:最新权重 + 历史检查点(rl/checkpoints/)+ 脚本 AI(baseline/hunter,经 dlopen 独立副本)。
- **验收**:只信真实引擎 `engine/build/runner`,双色各半,胜率 ≥90%,0 崩溃 0 超时。
