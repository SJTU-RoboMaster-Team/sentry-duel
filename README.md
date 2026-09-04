# 哨兵大战

上海交通大学交龙战队校内赛 AI 赛道的开源比赛平台，包含 7x7 回合制比赛引擎、网页对战与回放前端、示例 AI，以及可直接用于 PPO 自我对弈训练的强化学习环境。

在线赛事平台：<https://jiaoloong.sjtu.edu.cn/contest2026/>

## 仓库内容

```text
engine/             C++17 规则引擎、对局运行器和人机对战运行器
ai/                 Baseline/Hunter 示例与 RL 部署适配器
rl/env/             pybind11 向量化训练环境
rl/training/        PPO 训练、评测和 C++ 权重导出工具
server/             FastAPI 接口与浏览器前端
sentry_duel_docs/   比赛规则与选手 API 文档
cli/                本地对局运行器
```

仓库不包含训练权重、检查点、日志、编译产物、用户上传数据与生产环境部署配置。

## 环境要求

- Linux（对局运行器使用 POSIX 信号和 `dlopen`）
- CMake 3.10 或更高版本
- 支持 C++17 的编译器
- Python 3.10 或更高版本

运行网页平台需要安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
```

进行 RL 训练还需安装：

```bash
pip install -r requirements-rl.txt
```

## 构建并运行对局

```bash
cmake -S engine -B engine/build
cmake --build engine/build -j
make -C ai
python3 cli/run_match.py \
  --red ai/baseline_ai.so \
  --blue ai/hunter_ai.so \
  --game-id demo
```

## 运行网页平台

```bash
cd server
../.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。本地模式默认使用测试账号；只有将 `SENTRY_DUEL_AUTH_REQUIRED` 设为 `1` 时才会启用生产环境的 JAccount 登录。

## 强化学习训练

首先构建比赛引擎、脚本对手和 Python 扩展：

```bash
cmake -S engine -B engine/build
cmake --build engine/build -j
make -C ai
bash rl/env/build.sh
```

运行小规模冒烟训练：

```bash
python3 rl/training/train.py \
  --phase smoke \
  --n-envs 8 \
  --n-threads 4 \
  --steps-per-iter 4096 \
  --max-iters 5
```

运行联赛式自我对弈训练：

```bash
python3 rl/training/train.py \
  --phase league \
  --n-envs 64 \
  --n-threads 16 \
  --steps-per-iter 65536
```

观测、动作、奖励和二进制权重契约见 [RL 训练契约](rl/SPEC.md)。训练产物会写入 `rl/weights`、`rl/checkpoints`、`rl/logs` 和 `rl/runs`，这些目录均已被 Git 忽略。

将训练完成的策略导出到 C++ 选手运行时：

```bash
python3 rl/training/export_cpp.py \
  --weights rl/weights/final.sdw \
  --out ai/rl_weights.h
bash ai/build_rl_ai.sh
```

## 文档

- [完整比赛规则](sentry_duel_docs/rules.md)
- [C++ 选手 API](sentry_duel_docs/api.md)
- [RL 环境指南](rl/README.md)
- [RL 训练契约](rl/SPEC.md)

## 安全说明

选手源码和共享库都应视为不可信输入。仓库中的本地服务仅用于开发和演示。公网部署时，应增加操作系统级隔离、资源限制和带身份认证的反向代理。

## 开源许可

项目源码使用 [MIT 许可证](LICENSE)开源。`server/viewer/vendor` 中的第三方代码保留其原有许可说明。
