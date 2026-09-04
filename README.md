# Sentry Duel / 哨兵大战

上海交通大学交龙战队校内赛 AI 赛道的开源比赛平台，包含 7x7 回合制比赛引擎、网页对战与回放前端、示例 AI，以及可直接用于 PPO/self-play 训练的强化学习环境。

Online platform: <https://jiaoloong.sjtu.edu.cn/contest2026/>

## Repository Contents

```text
engine/             C++17 rules engine, match runner and human runner
ai/                 Baseline/Hunter examples and RL deployment adapter
rl/env/             pybind11 vectorized training environment
rl/training/        PPO training, evaluation and C++ weight export
server/             FastAPI API and browser UI
sentry_duel_docs/   Game rules and contestant API reference
cli/                Local match runner
```

Generated weights, checkpoints, logs, compiled libraries, user uploads and production deployment configuration are intentionally excluded.

## Prerequisites

- Linux (the match runner uses POSIX signals and `dlopen`)
- CMake 3.10+
- A C++17 compiler
- Python 3.10+

For the web platform:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
```

For RL training, additionally install:

```bash
pip install -r requirements-rl.txt
```

## Build And Run A Match

```bash
cmake -S engine -B engine/build
cmake --build engine/build -j
make -C ai
python3 cli/run_match.py \
  --red ai/baseline_ai.so \
  --blue ai/hunter_ai.so \
  --game-id demo
```

## Run The Web UI

```bash
cd server
../.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. Local mode uses a test account by default. The production JAccount integration is only enabled when `SENTRY_DUEL_AUTH_REQUIRED=1`.

## RL Training

Build the engine, scripted opponents and Python extension:

```bash
cmake -S engine -B engine/build
cmake --build engine/build -j
make -C ai
bash rl/env/build.sh
```

Run a small smoke training job:

```bash
python3 rl/training/train.py \
  --phase smoke \
  --n-envs 8 \
  --n-threads 4 \
  --steps-per-iter 4096 \
  --max-iters 5
```

Run league self-play:

```bash
python3 rl/training/train.py \
  --phase league \
  --n-envs 64 \
  --n-threads 16 \
  --steps-per-iter 65536
```

The observation, action, reward and binary weight contracts are documented in [rl/SPEC.md](rl/SPEC.md). Training outputs are written under `rl/weights`, `rl/checkpoints`, `rl/logs` and `rl/runs`, all ignored by Git.

Export a trained policy for the C++ contestant runtime:

```bash
python3 rl/training/export_cpp.py \
  --weights rl/weights/final.sdw \
  --out ai/rl_weights.h
bash ai/build_rl_ai.sh
```

## Documentation

- [Complete rules](sentry_duel_docs/rules.md)
- [C++ contestant API](sentry_duel_docs/api.md)
- [RL environment guide](rl/README.md)
- [RL contract](rl/SPEC.md)

## Security

Contestant source and shared libraries are untrusted input. The included local server is intended for development and demonstration. A public deployment should add operating-system isolation, resource limits and an authenticated reverse proxy.

## License

Project source is released under the [MIT License](LICENSE). Third-party code under `server/viewer/vendor` retains its own license notices.
