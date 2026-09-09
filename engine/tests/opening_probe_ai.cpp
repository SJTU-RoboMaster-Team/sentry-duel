#include "sentry_duel.h"

extern "C" void act(const Board& board, char my_color) {
    if (my_color != 'B' || board.turn != 0) return;
    const Sentry& opponent = board.red;
    const Sentry& me = board.blue;
    const bool unknown = me.last_known_pos.x == 0
                      && me.last_known_pos.y == 0
                      && me.last_known_facing == 'E'
                      && opponent.last_known_pos.x == -1
                      && opponent.last_known_pos.y == -1
                      && opponent.last_known_facing == '?'
                      && !opponent.visible;
    turn(unknown ? 'N' : 'S');
}
