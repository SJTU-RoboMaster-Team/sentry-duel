#include "match.h"
#include "sentry_duel.h"

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

} // namespace

int main() {
    run_initial_cases();
    run_respawn_cases();
    return 0;
}
