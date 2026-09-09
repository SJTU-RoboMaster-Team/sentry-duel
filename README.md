# 哨兵大战 · 引擎与可视化

7×7 网格回合制 1v1 AI 对战游戏。规则参见 `../sentry_duel_docs/`。

## 目录结构

```
sentry_duel_engine/
├── engine/           # C++ 引擎核心(选手 ABI + 规则引擎 + 调度)
├── ai/               # 选手 AI 示例(随机、反应式、占点+攻击)
├── cli/              # CLI 跑一局 → 录像 JSON
├── server/           # FastAPI 可视化 + SSE 实时推送
├── replays/          # 录像输出
└── README.md
```

## 编译与运行

### 1. 编译引擎

```bash
cd engine
mkdir build && cd build
cmake ..
make
```

产物:`engine/build/runner` + `engine/build/libsentry_duel_engine.so`

### 2. 编译 AI

```bash
cd ai
make
```

产物:`ai/baseline_ai.so`、`ai/hunter_ai.so`

### 3. 跑一局(CLI)

```bash
cd sentry_duel_engine
python3 cli/run_match.py --red ai/baseline_ai.so --blue ai/hunter_ai.so --game-id demo1
```

输出:`replays/game_demo1.json`

### 4. 启动可视化服务

```bash
# 安装依赖(一次性)
pip install --user --break-system-packages fastapi uvicorn

# 启动
cd server
uvicorn app:app --reload --port 8000

# 重启服务
python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://localhost:8000/` 看最新一场;或 `/viewer/demo1` 看指定场。

观战页可直接选择红蓝 AI、设置最大回合数并点击“开始对局”，对局会通过 SSE 实时展示。
加载已有录像后，可用“自动回放”按 1×、2× 或 5×速度播放，也可随时暂停和单步查看。

页面顶部的“人机模式”可选择已上传 AI、红/蓝阵营和回合上限。每回合玩家最多连续完成 3 个消耗动作；游戏开始后及击杀复活后的第一次行动中，尚未离开出生点时首次转向免费。可按 `SPACE` 主动结束回合。玩家按棋盘方向使用 `W` 上、`A` 左、`S` 下、`D` 右：未朝向目标方向时先转向，再次按下才移动；`Q` 扫描、`E` 开火。顶部会显示玩家 FIRE/SCAN 冷却。动作仍由同一引擎规则实时结算。

网页端还提供以下赛事功能：

- “AI 代码库”集中管理本人 AI，并浏览、下载其他选手主动公开的源码。
- “批量测试”让两个可见 AI 双方各执红方 50 局，汇总 100 局结果。
- “AI 排行榜”默认包含官方 Baseline 与 Hunter；每个 JAccount 默认保留一个榜位，每天最多提交 10 次。提交 AI 后，它会与当前榜内所有其他榜位各进行 20 局平衡对战（红蓝各 10 局），全部成功后动态更新排名；提交时可选择匿名显示选手身份，系统每天保留一份官方榜单快照。
- 排行榜按综合得分率 `(胜局 + 0.5 × 平局) / 总局数` 排序，并标记该榜单版本是否开源。选择开源时，下载内容固定为本次打榜时的源码快照。

## 自己写 AI

复制 `ai/baseline_ai.cpp`,改 `act()` 函数:

```cpp
#include "sentry_duel.h"
#include "utils.h"

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me  = (my_color == 'R') ? board.red  : board.blue;
    const Sentry& opp = (my_color == 'R') ? board.blue : board.red;

    // 你的策略:看到人就打
    if (opp.visible && me.fire_cd == 0) {
        fire();
    }
    // 看不到就雷达
    if (me.scan_cd == 0) {
        scan();
    }
    // 朝中心走
    move();
}
```

编译:

```bash
g++ -std=c++17 -O2 -fPIC -shared -Wl,-z,lazy -Wl,--allow-shlib-undefined \
    -I../engine/include -L../engine/build \
    -Wl,-rpath,'$ORIGIN/../engine/build' -lsentry_duel_engine \
    your_ai.cpp -o your_ai.so
```

参赛选手**只需** `engine/include/sentry_duel.h` 和 `engine/include/utils.h` 两个头文件 + 链接 `libsentry_duel_engine.so`。

## 事件格式

引擎每行输出一个 JSON(也写入录像):

| 类型 | 字段 |
|---|---|
| `start` | game_id, red, blue, size |
| `turn_start` | turn |
| `action` | turn, side, action(`move`/`turn`/`fire`/`scan`), arg(可选), success |
| `turn_end` | turn, red_pos, blue_pos, red_score, blue_score, red_visible, blue_visible |
| `game_over` | winner(`R`/`B`/`D`), reason, red_score, blue_score, turns |

## 安全约束

- 单次 `act()` 调用 ≤ 1 秒；超时方放弃该回合，对手立即 +1 分
- 崩溃(SIGSEGV)整局判负
- 选手 .so 调用 `move/turn/fire/scan` 由引擎解析,**不可直接传 `.so` 路径绕过引擎**

## 已知问题与后续工作

- [x] 蓝方 180° 镜像视角
- [x] 平分时最多 5 个完整加时回合
- [ ] 系统层沙箱(firejail/namespace)用于开放上传
- [ ] 录像压缩 / 二进制格式 / 回放控制(快进、倒退、暂停)
