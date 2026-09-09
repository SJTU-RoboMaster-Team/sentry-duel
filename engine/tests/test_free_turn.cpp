#include "match.h"
#include "sentry_duel.h"
#include "engine_internal.h"

#include <cstdlib>
#include <iostream>

namespace {

struct Results {
    ActionResult initial_turn{};
    ActionResult initial_move{};
    ActionResult turn_after_initial_move{};
    ActionResult respawn_turn{};
    ActionResult respawn_move{};
    ActionResult turn_after_respawn_move{};
    int red_score_seen_by_blue = -1;
    Board blue_opening_view{};
    ActionResult fire_after_scan{};
    ActionResult fourth_action{};
};

void require(bool condition, const char* message) {
    if (condition) return;
    std::cerr << message << '\n';
    std::exit(1);
}

void run_initial_cases() {
    Results results;
    int free_phase = 0;
    sentry::Match free_at_spawn(
        [&](const Board&, char) {
            if (free_phase++ == 0) results.initial_turn = turn('N');
            return 0;
        },
        [](const Board&, char) { return 0; }, "red", "blue", 1);
    free_at_spawn.run();
    require(results.initial_turn.success, "initial turn should succeed");
    require(!results.initial_turn.consumed, "initial turn at spawn should be free");

    int leave_phase = 0;
    sentry::Match leave_spawn(
        [&](const Board&, char) {
            if (leave_phase++ == 0) {
                results.initial_move = move();
                results.turn_after_initial_move = turn('N');
            }
            return 0;
        },
        [](const Board&, char) { return 0; }, "red", "blue", 1);
    leave_spawn.run();
    require(results.initial_move.success && results.initial_move.consumed,
            "initial move should consume an action");
    require(results.turn_after_initial_move.success,
            "turn after initial move should succeed");
    require(results.turn_after_initial_move.consumed,
            "turn after leaving initial spawn should consume an action");
}

void run_respawn_case(bool leave_spawn, Results& results) {
    int red_phase = 0;
    int blue_phase = 0;
    sentry::Match match(
        [&](const Board&, char) {
            if (red_phase == 0) {
                move(); move(); move();
            } else if (red_phase == 1) {
                turn('S'); move(); move();
            } else if (red_phase == 2) {
                const ActionResult shot = fire();
                require(shot.success, "setup shot should respawn blue");
            }
            ++red_phase;
            return 0;
        },
        [&](const Board&, char) {
            if (blue_phase == 0) {
                move(); move(); move();
            } else if (blue_phase == 1) {
                turn('S'); move(); move();
            } else if (blue_phase == 2) {
                if (leave_spawn) {
                    results.respawn_move = move();
                    results.turn_after_respawn_move = turn('N');
                } else {
                    results.respawn_turn = turn('N');
                }
            }
            ++blue_phase;
            return 0;
        },
        "red", "blue", 3);
    match.run();
}

void run_respawn_cases() {
    Results at_spawn;
    run_respawn_case(false, at_spawn);
    require(at_spawn.respawn_turn.success, "turn after respawn should succeed");
    require(!at_spawn.respawn_turn.consumed, "turn at respawn point should be free");

    Results after_move;
    run_respawn_case(true, after_move);
    require(after_move.respawn_move.success && after_move.respawn_move.consumed,
            "move after respawn should consume an action");
    require(after_move.turn_after_respawn_move.success,
            "turn after leaving respawn point should succeed");
    require(after_move.turn_after_respawn_move.consumed,
            "turn after leaving respawn point should consume an action");
}

void run_side_turn_scoring_case() {
    Results results;
    int red_phase = 0;
    int blue_phase = 0;
    sentry::Match match(
        [&](const Board&, char) {
            if (red_phase == 0) {
                move(); move(); move();
            } else if (red_phase == 1) {
                turn('S'); move(); move();
            }
            ++red_phase;
            return 0;
        },
        [&](const Board& board, char) {
            if (blue_phase == 1) {
                results.red_score_seen_by_blue = board.red.score;
            }
            ++blue_phase;
            return 0;
        },
        "red", "blue", 2);
    match.run();
    require(results.red_score_seen_by_blue == 1,
            "red should score before the following blue action phase");

    Board board = make_initial_board(7);
    board.red.last_known_pos = {3, 2};
    board.blue.last_known_pos = {3, 3};
    end_side_turn(board, 'R');
    end_side_turn(board, 'B');
    require(board.red.score == 1 && board.blue.score == 1,
            "each side in the score zone should score at its own turn end");
}

void run_opening_intel_case() {
    Results results;
    sentry::Match match(
        [](const Board&, char) { move(); move(); move(); return 0; },
        [&](const Board& board, char) { results.blue_opening_view = board; return 0; },
        "red", "blue", 1);
    match.run();

    const Sentry& opponent = results.blue_opening_view.red;
    const Sentry& me = results.blue_opening_view.blue;
    require(me.last_known_pos.x == 0 && me.last_known_pos.y == 0
            && me.last_known_facing == 'E',
            "blue should receive the 180-degree mirrored local view");
    require(opponent.last_known_pos.x == -1 && opponent.last_known_pos.y == -1,
            "blue should not receive red position on the opening turn");
    require(opponent.last_known_facing == '?',
            "blue should not receive red facing on the opening turn");
    require(!opponent.visible,
            "red should not be visible to blue on the opening turn");
}

void run_scan_visibility_case() {
    Results results;
    int red_phase = 0;
    int blue_phase = 0;
    sentry::Match match(
        [&](const Board&, char) {
            if (red_phase == 0) {
                move(); move(); move();
            } else if (red_phase == 1) {
                turn('S'); move(); move();
            } else if (red_phase == 2) {
                const ScanResult scanned = scan();
                require(scanned.success && scanned.observation.opp_visible,
                        "scan should reveal the opponent");
                results.fire_after_scan = fire();
            }
            ++red_phase;
            return 0;
        },
        [&](const Board&, char) {
            if (blue_phase == 0) {
                move(); move(); move();
            } else if (blue_phase == 1) {
                turn('S'); move(); move();
            }
            ++blue_phase;
            return 0;
        },
        "red", "blue", 3);
    match.run();

    require(results.fire_after_scan.success,
            "fire after scan should succeed");
    require(results.fire_after_scan.observation.opp_visible,
            "scan visibility should last for the remainder of act()");
    require(results.fire_after_scan.observation.opp_last_known_pos.x == 6
            && results.fire_after_scan.observation.opp_last_known_pos.y == 6,
            "post-hit scan observation should contain the respawn position");
}

void run_action_limit_case() {
    Results results;
    int red_phase = 0;
    sentry::Match match(
        [&](const Board&, char) {
            if (red_phase++ == 0) {
                move(); move(); move();
                results.fourth_action = turn('S');
            }
            return 0;
        },
        [](const Board&, char) { return 0; },
        "red", "blue", 1);
    match.run();
    require(!results.fourth_action.success && !results.fourth_action.consumed,
            "a fourth consumed action should be rejected");
}

void run_cooldown_and_respawn_case() {
    Board board = make_initial_board(7);
    board.red.last_known_pos = {3, 2};
    board.red.last_known_facing = 'S';
    board.red.scan_cd = 2;
    board.blue.last_known_pos = {3, 4};
    board.blue.last_known_facing = 'N';
    board.blue.fire_cd = 1;

    const ActionOutcome shot = apply_action(board, 'R', 2, 0);
    require(shot.success && shot.hit, "fire should hit inside its unobstructed lane");
    require(board.blue.last_known_pos.x == 6 && board.blue.last_known_pos.y == 6,
            "a killed blue sentry should respawn at its spawn");
    require(board.blue.last_known_facing == 'W',
            "a killed blue sentry should restore its initial facing");
    require(board.blue.fire_cd == 1,
            "respawn should preserve the killed sentry fire cooldown");
    require(board.red.scan_cd == 2,
            "firing should not change the shooter's scan cooldown");

    end_round(board);
    require(board.red.fire_cd == 1 && board.red.scan_cd == 1
            && board.blue.fire_cd == 0,
            "all positive cooldowns should decrement once at round end");
    end_round(board);
    require(board.red.fire_cd == 0 && board.red.scan_cd == 0,
            "fire should recover after one full later round");

    const ActionOutcome scanned = apply_action(board, 'R', 3, 0);
    require(scanned.success && board.red.scan_cd == 3,
            "scan should start a three-round cooldown");
    require(!apply_action(board, 'R', 3, 0).success,
            "scan should fail while its cooldown is positive");
}

void run_invalid_action_case() {
    ActionResult invalid_turn{};
    ActionResult same_facing_turn{};
    ActionResult third_consumed_action{};
    int red_phase = 0;
    sentry::Match match(
        [&](const Board&, char) {
            if (red_phase++ == 0) {
                invalid_turn = turn('X');
                same_facing_turn = turn('E');
                move();
                third_consumed_action = turn('S');
            }
            return 0;
        },
        [](const Board&, char) { return 0; },
        "red", "blue", 1);
    match.run();

    require(!invalid_turn.success && !invalid_turn.consumed,
            "an invalid turn should not consume an action");
    require(same_facing_turn.success && !same_facing_turn.consumed,
            "the opening same-facing turn at spawn should succeed for free");
    require(third_consumed_action.success && third_consumed_action.consumed,
            "an earlier invalid action should not reduce the action allowance");
}

void run_timeout_and_crash_case() {
    sentry::Match timeout_match(
        [](const Board&, char) { return 1; },
        [](const Board&, char) { return 0; },
        "red", "blue", 1);
    require(timeout_match.run() == 2,
            "the opponent should receive one point when red times out");
    require(timeout_match.board().blue.score == 1,
            "a timeout should award exactly one penalty point");

    sentry::Match crash_match(
        [](const Board&, char) { return 2; },
        [](const Board&, char) { return 0; },
        "red", "blue", 20);
    require(crash_match.run() == 2 && crash_match.reason() == "red_crashed",
            "a crashing red policy should immediately lose the game");
    require(crash_match.board().turn == 0,
            "a crash should end the game before completing the round");
}

void run_overtime_limit_case() {
    sentry::Match match(
        [](const Board&, char) { return 0; },
        [](const Board&, char) { return 0; },
        "red", "blue", 20);
    const int winner = match.run();
    require(winner == 3 && match.reason() == "overtime_draw",
            "a tie after five overtime rounds should be a draw");
    require(match.board().turn == 25,
            "regulation plus overtime should stop after 25 complete rounds");
}

} // namespace

int main() {
    run_initial_cases();
    run_respawn_cases();
    run_side_turn_scoring_case();
    run_opening_intel_case();
    run_scan_visibility_case();
    run_action_limit_case();
    run_cooldown_and_respawn_case();
    run_invalid_action_case();
    run_timeout_and_crash_case();
    run_overtime_limit_case();
    return 0;
}
