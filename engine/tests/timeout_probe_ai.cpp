#include "sentry_duel.h"

#include <chrono>

extern "C" void act(const Board&, char my_color) {
    if (my_color != 'R') return;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(1500);
    while (std::chrono::steady_clock::now() < deadline) {
    }
}
