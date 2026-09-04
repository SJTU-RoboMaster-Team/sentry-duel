// det_ai_b.cpp - 确定性测试 AI(偏游走,与 det_ai_a 风格明显不同)
// 策略:S 形蛇形走位(偶数行向东、奇数行向西,受阻依次尝试南/反向/北),
//       每 3 回合定期 SCAN 一次,只有直接看到敌人才转向开火。
// 不用任何时间/随机源,行为只依赖 (board, my_color),保证 trace 可复现。

#include "sentry_duel.h"
#include "utils.h"

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me  = (my_color == 'R') ? board.red  : board.blue;
    const Sentry& opp = (my_color == 'R') ? board.blue : board.red;
    const int t = board.turn;

    int used = 0;
    Pos pos = me.last_known_pos;
    char facing = me.last_known_facing;
    int fire_cd = me.fire_cd;
    int scan_cd = me.scan_cd;
    bool opp_visible = opp.visible;
    Pos opp_pos = opp.last_known_pos;

    // act() 期间用最近一次行动的观测更新本地状态(board 快照本身不会随行动刷新)
    auto track = [&](const ActionResult& r) {
        if (r.consumed) ++used;
        pos = r.observation.my_pos;
        facing = r.observation.my_facing;
        fire_cd = r.observation.fire_cd;
        scan_cd = r.observation.scan_cd;
        opp_visible = r.observation.opp_visible;
        if (r.observation.opp_last_known_pos.x >= 0)
            opp_pos = r.observation.opp_last_known_pos;
    };

    // 1. 只有看到敌人才开火(不做 a 那种 SCAN 后估算打击)
    if (opp_visible && fire_cd == 0) {
        const char dir = best_turn_to_face(pos, opp_pos);
        if (facing != dir && used < 3) {
            const ActionResult r = turn(dir);
            track(r);
        }
        if (used < 3 && facing == dir) {
            const ActionResult r = fire();
            track(r);
        }
    }

    // 2. 定期 SCAN(每 3 回合一次,与 a 的"CD 好就扫"节奏不同)
    if (t % 3 == 2 && scan_cd == 0 && used < 3) {
        const ScanResult r = scan();
        track(ActionResult{r.success, r.consumed, r.observation});
    }

    // 3. S 形蛇形走位
    while (used < 3) {
        const char horizontal = (pos.y % 2 == 0) ? 'E' : 'W';
        const char candidates[4] = {horizontal, 'S',
                                    horizontal == 'E' ? 'W' : 'E', 'N'};
        bool moved = false;
        for (int i = 0; i < 4 && used < 3; ++i) {
            if (facing != candidates[i]) {
                const ActionResult rt = turn(candidates[i]);
                track(rt);
                if (!rt.success) break;      // 行动次数已用完
            }
            const ActionResult rm = move();
            track(rm);
            if (rm.success) { moved = true; break; }
            // 撞墙/出界/撞人:失败不消耗,换下一个候选方向
        }
        if (!moved) break;
    }
}
