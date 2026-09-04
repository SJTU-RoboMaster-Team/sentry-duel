// det_ai_a.cpp - 确定性测试 AI(偏进攻)
// 策略:SCAN→TURN→FIRE 连招,朝中心推进,进得分区后驻守。
// 前两回合固定插入失败路径探测:非法转向字符、CD 中 FIRE/SCAN、撞墙、出界。
// 不用任何时间/随机源,行为只依赖 (board, my_color),保证 trace 可复现。

#include "sentry_duel.h"
#include "utils.h"

namespace {

// act() 期间用最近一次行动的观测更新本地状态(board 快照本身不会随行动刷新)
struct LocalState {
    int used = 0;             // 本回合已消耗行动数
    Pos pos{0, 0};
    char facing = 'E';
    int fire_cd = 0;
    int scan_cd = 0;
    bool opp_visible = false;
    Pos opp_pos{-1, -1};
};

void track(LocalState& s, const ActionResult& r) {
    if (r.consumed) ++s.used;
    s.pos = r.observation.my_pos;
    s.facing = r.observation.my_facing;
    s.fire_cd = r.observation.fire_cd;
    s.scan_cd = r.observation.scan_cd;
    s.opp_visible = r.observation.opp_visible;
    if (r.observation.opp_last_known_pos.x >= 0)
        s.opp_pos = r.observation.opp_last_known_pos;
}

void track(LocalState& s, const ScanResult& r) {
    track(s, ActionResult{r.success, r.consumed, r.observation});
}

} // namespace

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me  = (my_color == 'R') ? board.red  : board.blue;
    const Sentry& opp = (my_color == 'R') ? board.blue : board.red;
    const int t = board.turn;

    LocalState s;
    s.pos = me.last_known_pos;
    s.facing = me.last_known_facing;
    s.fire_cd = me.fire_cd;
    s.scan_cd = me.scan_cd;
    s.opp_visible = opp.visible;
    s.opp_pos = opp.last_known_pos;

    // —— 失败路径探测(失败不消耗行动次数)——
    if (t == 0) {
        turn(my_color == 'R' ? 'X' : 'n');   // 非法转向字符,被拒
        track(s, scan());                    // 成功,消耗 1
        scan();                              // SCAN CD 中,被拒
        track(s, fire());                    // 成功,消耗 1
        fire();                              // FIRE CD 中,被拒
    } else if (t == 1) {
        fire();                              // 上回合开过火,CD 未结束,被拒
        track(s, turn('N'));                 // 消耗 1
        move();                              // 出界,被拒(镜像视图下双方都在上边缘附近)
    }

    // —— 主策略 ——
    while (s.used < 3) {
        // 1. 掌握敌情且可开火:转向 → 开火
        if (s.opp_visible && s.opp_pos.x >= 0 && s.fire_cd == 0) {
            const char dir = best_turn_to_face(s.pos, s.opp_pos);
            if (s.facing != dir) {
                const ActionResult r = turn(dir);
                track(s, r);
                if (!r.success) break;
                continue;
            }
            const ActionResult r = fire();
            track(s, r);
            if (!r.success) break;
            continue;
        }
        // 2. 雷达可用就扫描
        if (s.scan_cd == 0) {
            const ScanResult r = scan();
            track(s, r);
            if (!r.success) break;
            continue;
        }
        // 3. 进得分区则驻守
        if (in_score_zone(s.pos, board.score_zones)) break;
        // 4. 朝中心推进,被挡则横向绕行
        const Pos center{3, 3};
        const char dir = best_turn_to_face(s.pos, center);
        if (s.facing != dir) {
            const ActionResult r = turn(dir);
            track(s, r);
            if (!r.success) break;
            continue;
        }
        const ActionResult r = move();
        track(s, r);
        if (!r.success) {
            const char alt = (dir == 'E' || dir == 'W') ? 'S' : 'E';
            if (s.used >= 3) break;
            if (s.facing != alt) {
                const ActionResult rt = turn(alt);
                track(s, rt);
                if (!rt.success) break;
            }
            if (s.used >= 3) break;
            const ActionResult rm = move();
            track(s, rm);
            if (!rm.success) break;
        }
    }
}
