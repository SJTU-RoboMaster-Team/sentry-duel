#pragma once

#include "sentry_duel.h"

#include <array>
#include <cstdlib>
#include <queue>
#include <vector>

inline bool same_pos(const Pos& left, const Pos& right) {
    return left.x == right.x && left.y == right.y;
}

inline bool blocked(const Board& board, int x, int y, const Pos& opponent) {
    if (x < 0 || x >= board.size || y < 0 || y >= board.size) return true;
    if (x == opponent.x && y == opponent.y) return true;
    for (const Pos& obstacle : board.obstacles) {
        if (obstacle.x == x && obstacle.y == y) return true;
    }
    return false;
}

inline char next_move_direction(const Board& board, const Pos& from,
                                const Pos& target, const Pos& opponent) {
    if (same_pos(from, target)) return '?';

    constexpr std::array<char, 4> directions = {'N', 'E', 'S', 'W'};
    constexpr std::array<int, 4> dx = {0, 1, 0, -1};
    constexpr std::array<int, 4> dy = {-1, 0, 1, 0};
    std::vector<std::vector<int>> distance(board.size,
                                           std::vector<int>(board.size, -1));
    std::vector<std::vector<char>> first_step(board.size,
                                               std::vector<char>(board.size, '?'));
    std::queue<Pos> frontier;
    frontier.push(from);
    distance[from.y][from.x] = 0;
    Pos closest = from;
    int closest_distance = std::abs(from.x - target.x) + std::abs(from.y - target.y);

    while (!frontier.empty()) {
        Pos current = frontier.front();
        frontier.pop();
        if (same_pos(current, target)) return first_step[current.y][current.x];

        for (int index = 0; index < 4; ++index) {
            const int next_x = current.x + dx[index];
            const int next_y = current.y + dy[index];
            if (blocked(board, next_x, next_y, opponent) || distance[next_y][next_x] >= 0) continue;
            distance[next_y][next_x] = distance[current.y][current.x] + 1;
            first_step[next_y][next_x] = distance[current.y][current.x] == 0
                ? directions[index]
                : first_step[current.y][current.x];
            const int target_distance = std::abs(next_x - target.x) + std::abs(next_y - target.y);
            if (target_distance < closest_distance) {
                closest = {next_x, next_y};
                closest_distance = target_distance;
            }
            frontier.push({next_x, next_y});
        }
    }
    return first_step[closest.y][closest.x];
}

inline void direction_delta(char direction, int& dx, int& dy) {
    dx = 0;
    dy = 0;
    if (direction == 'N') dy = -1;
    if (direction == 'E') dx = 1;
    if (direction == 'S') dy = 1;
    if (direction == 'W') dx = -1;
}

inline bool in_fire_range(const Pos& from, char facing, const Pos& target) {
    const int dx = target.x - from.x;
    const int dy = target.y - from.y;
    if (facing == 'N') return dy <= -1 && dy >= -3 && dx >= -1 && dx <= 1;
    if (facing == 'E') return dx >= 1 && dx <= 3 && dy >= -1 && dy <= 1;
    if (facing == 'S') return dy >= 1 && dy <= 3 && dx >= -1 && dx <= 1;
    if (facing == 'W') return dx <= -1 && dx >= -3 && dy >= -1 && dy <= 1;
    return false;
}

inline bool in_known_fire_coverage(const Pos& enemy, char enemy_facing,
                                   const Pos& candidate) {
    return enemy.x >= 0 && enemy.y >= 0 &&
           in_fire_range(enemy, enemy_facing, candidate);
}

// Simulates each planned successful action locally, so an AI can spend its
// remaining budget on consecutive safe path steps.
inline int advance_toward(const Board& board, Pos& position, char& facing,
                          const Pos& opponent, const Pos& target, int budget,
                          ActionObservation* observation = nullptr,
                          bool stop_on_direct_visibility = true,
                          const Pos* known_enemy = nullptr,
                          char known_enemy_facing = '?') {
    int spent = 0;
    while (spent < budget) {
        Board navigation_board = board;
        if (known_enemy && known_enemy->x >= 0 && known_enemy->y >= 0) {
            for (int y = 0; y < board.size; ++y) {
                for (int x = 0; x < board.size; ++x) {
                    const Pos candidate = {x, y};
                    if (in_known_fire_coverage(*known_enemy, known_enemy_facing, candidate) &&
                        !same_pos(candidate, target)) {
                        navigation_board.obstacles.push_back(candidate);
                    }
                }
            }
        }
        const char direction = next_move_direction(navigation_board, position, target, opponent);
        if (direction == '?') break;
        if (facing != direction) {
            const ActionResult result = turn(direction);
            if (observation) *observation = result.observation;
            if (!result.success) break;
            facing = direction;
            if (result.consumed) ++spent;
            if (spent >= budget) break;
            // SCAN 的临时敌情不应中断路径；只有新获得的直接视野才交回策略层。
            if (stop_on_direct_visibility && result.observation.opp_directly_visible) break;
        }
        int dx, dy;
        direction_delta(direction, dx, dy);
        const ActionResult result = move();
        if (observation) *observation = result.observation;
        if (!result.success) break;
        position.x += dx;
        position.y += dy;
        if (result.consumed) ++spent;
        // MOVE 后的实时观测优先于预先规划的下一步路径。
        if (stop_on_direct_visibility && result.observation.opp_directly_visible) break;
    }
    return spent;
}

inline int move_toward(const Board& board, const Sentry& me, const Pos& opponent,
                       const Pos& target) {
    Pos position = me.last_known_pos;
    char facing = me.last_known_facing;
    return advance_toward(board, position, facing, opponent, target, 2);
}
