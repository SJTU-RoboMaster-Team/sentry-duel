# 哨兵大战 AI 接口规范

本文档以当前引擎实现为准。普通提交默认接收一份 C++ AI 源码；房间对战和 AI 代码库也支持第 9 节所述的多文件源码包。

## 1. 必须实现的函数

```cpp
#include "sentry_duel.h"

extern "C" void act(const Board& board, char my_color) {
    // 在此调用 move / turn / fire / scan
}
```

引擎每回合分别调用红、蓝双方一次 `act()`。函数必须在 1 秒内返回，且不能定义 `main()`。超时会结束本方当前行动阶段、作废尚未执行的代码并令对方 +1；超时前已经成功结算的行动不会回滚，比赛继续。段错误或未处理异常则判该局负。

## 2. 坐标与视角

- 棋盘固定为 7 x 7，坐标 `x` 从左到右、`y` 从上到下，范围均为 `[0, 7)`。
- 红方先行动，蓝方后行动。
- 引擎对蓝方自动做 180 度镜像。因此双方写 AI 时都以“我方出生在 `(0, 0)`、初始朝向 `E`，敌方出生在 `(6, 6)`、初始朝向 `W`”为准。
- 仍需用 `my_color` 从 `board.red` 和 `board.blue` 中取出自己和对手。

```cpp
const Sentry& me = my_color == 'R' ? board.red : board.blue;
const Sentry& opp = my_color == 'R' ? board.blue : board.red;
```

## 3. 数据结构

```cpp
struct Pos {
    int x;
    int y;
};

struct Sentry {
    Pos last_known_pos;
    char last_known_facing;  // 'N' / 'E' / 'S' / 'W' / '?'
    bool visible;
    int fire_cd;
    int scan_cd;
    int score;
};

struct Board {
    Sentry red;
    Sentry blue;
    int turn;
    int size;                    // 恒为 7
    std::vector<Pos> obstacles;
    std::vector<Pos> score_zones;
};
```

`board` 是回合开始时的只读快照。行动发生后它不会更新，应读取每个行动返回的 `observation` 获取实时状态。

对自己：位置、朝向、CD 和得分始终有效。对对手：

- `opp.visible == true`：对手当前在直接视野内，位置和朝向为实时值。
- `opp.visible == false`：位置和朝向只是最后一次已知情报；从未得知时为 `{-1, -1}` 与 `'?'`。
- 对方的 `fire_cd`、`scan_cd` 不会暴露，值为 `-1`。

## 4. 行动接口

```cpp
struct ActionObservation {
    Pos my_pos;
    char my_facing;
    Pos opp_last_known_pos;
    char opp_last_known_facing;
    bool opp_visible;
    bool opp_directly_visible;
    int fire_cd;
    int scan_cd;
};

struct ActionResult {
    bool success;
    bool consumed;
    ActionObservation observation;
};

struct ScanResult {
    bool success;
    bool consumed;
    ActionObservation observation;
};

ActionResult move();
ActionResult turn(char direction);
ActionResult fire();
ScanResult scan();
```

每回合最多消耗 3 次成功行动。失败调用不消耗次数。`consumed` 表示这次成功调用是否消耗额度。

### `move()`

向当前朝向移动一格。目标出界、是障碍或被对方占据时失败。

### `turn('N' | 'E' | 'S' | 'W')`

立即设置朝向。非法字符失败；转向到当前朝向也是成功行动并消耗次数。游戏开始后或被击杀复活后的下一次 `act()` 中，只有仍在出生点时执行的首个成功 `turn()` 免费，返回的 `consumed` 为 `false`；一旦离开出生点，免费资格立即失效。

### `fire()`

只要求 `fire_cd == 0`。允许在没有敌方视野时盲射。

- 火力范围是朝向前方距离 1 至 3 的 3 x 3 区域。
- 障碍会阻挡同一火力通道上障碍之后的格子。
- 命中后敌方立即回到出生点、朝向重置，开火方加 2 分。
- 当前实现中一次开火会将 `fire_cd` 设为 2，并在每回合结束减 1；因此下一个己方回合不能开火，再下一个己方回合恢复可用。
- 开火是否命中都算一次成功行动并进入冷却。

### `scan()`

只要求 `scan_cd == 0`。成功后立即返回敌方实时位置与朝向，并在本次 `act()` 剩余时间内令 `observation.opp_visible` 为 `true`。

- 成功扫描后 `scan_cd` 设为 3，每回合结束减 1。
- 下一回合开始时，临时扫描视野消失；敌方是否可见重新由直接视野决定。

## 5. 行动后的实时决策

```cpp
extern "C" void act(const Board& board, char my_color) {
    const Sentry& me = my_color == 'R' ? board.red : board.blue;

    if (me.scan_cd == 0) {
        ScanResult result = scan();
        if (result.success && result.observation.fire_cd == 0) {
            turn(best_turn_to_face(result.observation.my_pos,
                                   result.observation.opp_last_known_pos));
            fire();
        }
    }
}
```

一次 `act()` 内的调用顺序立即生效。`ActionObservation` 中的坐标、朝向、CD 与可见性是这次调用结算后的状态。

## 6. 可用工具函数

```cpp
#include "utils.h"

bool can_see(const Sentry& me, const Pos& target,
             const std::vector<Pos>& obstacles);
int manhattan_distance(const Pos& a, const Pos& b);
bool in_score_zone(const Pos& pos, const std::vector<Pos>& score_zones);
char best_turn_to_face(const Pos& from, const Pos& to);
bool can_move_forward(const Sentry& me, const Pos& opp_last_known_pos,
                      const std::vector<Pos>& obstacles, int board_size);
```

## 7. 视野与回合要点

- 直接视野为 T 形：正前方一格，加上距离二的横向三格；障碍会遮挡视线。
- 视野与火力范围不同。看不到敌人也允许 `fire()`，但只有敌人实际落在火力范围内才会命中。
- 每方 `act()` 返回时，若该方位于得分区则立即获得 1 分；双方分别结算，互不排斥。双方行动结束后，双方 CD 统一递减。
- 红方回合 0 先行动；首回合不向蓝方提供红方的位置或朝向情报。双方敌方状态均从 `{-1, -1}` 与 `'?'` 开始，直到通过视野或 `scan()` 获得情报。
- 游戏开始后或被击杀复活后的下一次行动中，只有尚未离开出生点时的首个有效 `turn()` 免费；离开出生点后免费资格失效。复活不会清除其 FIRE 或 SCAN 冷却。
- 常规 20 回合结束后若平分，最多进行 5 个完整加时回合。每个加时回合在双方各自的行动结束计分完成后比较比分；不再平分时分数较高者获胜。

## 8. 最小示例

```cpp
#include "sentry_duel.h"

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me = my_color == 'R' ? board.red : board.blue;
    const Sentry& opp = my_color == 'R' ? board.blue : board.red;

    if (opp.visible && me.fire_cd == 0) {
        fire();
        return;
    }
    if (me.scan_cd == 0) {
        scan();
    }
    move();
}
```

## 9. 多文件源码包提交

除单文件源码外,房间对战还支持以压缩包形式提交多文件项目:
`POST /api/rooms/{code}/upload_pack`(表单字段 `side` / `upload_token` / `name` / `display` / `file`)。

**压缩包要求**

- 格式:`.zip` / `.tar` / `.tar.gz` / `.tgz`;压缩包 ≤20MB
- 文件数 ≤64,单文件 ≤8MB,解压后总大小 ≤20MB
- 包内不得包含符号链接、绝对路径或 `../` 越界路径,否则直接拒绝
- 源码可放在包根,或外层只套一层目录(服务端自动向下定位项目根,最多两层)

**构建规则**

- 项目根有 `Makefile` 时执行 `make`:必须把 `.so` 产出到项目根目录;
  Makefile 内可使用环境变量 `$(ENGINE_INCLUDE)`(引擎头文件目录)和
  `$(ENGINE_LIB_DIR)`(引擎库目录)
- 没有 `Makefile` 时,用标准命令编译项目根那一层的全部 `.cpp`
  (更深层子目录的源码不参与编译,请在根层 `.cpp` 中组织好代码)
- 主源码优先取 `my_ai.cpp`,它决定 AI 列表中展示的源码
- 编译限时 120 秒;失败会返回编译器输出,房间状态不受影响,修正后可重新上传

## 10. 入围赛技术文档

技术文档是入围材料，但不作为发起技术评测或显示已有入围结果的前置条件。支持 PDF、DOCX 和 Markdown，单文件不超过 20MB；每个账号保留最新上传的一份。

- 上传：`POST /api/qualifiers/document`，`multipart/form-data` 字段为 `file`
- 下载本人文档：`GET /api/qualifiers/document/download`
- 查询提交状态：`GET /api/qualifiers` 返回 `technical_document`；未提交时为 `null`
- 发起评测：`POST /api/qualifiers`。是否已提交技术文档不影响该接口
