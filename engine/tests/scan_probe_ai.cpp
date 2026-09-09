#include "sentry_duel.h"

extern "C" void act(const Board& board, char my_color) {
    if (board.turn == 0) {
        move(); move(); move();
        return;
    }
    if (board.turn == 1) {
        turn('S'); move(); move();
        return;
    }
    if (board.turn == 2 && my_color == 'R') {
        scan();
        const ActionResult shot = fire();
        const bool retained = shot.observation.opp_visible
                           && shot.observation.opp_last_known_pos.x == 6
                           && shot.observation.opp_last_known_pos.y == 6;
        turn(retained ? 'N' : 'S');
    }
}
