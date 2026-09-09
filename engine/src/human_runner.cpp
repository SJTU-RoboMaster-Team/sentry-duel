// 交互式人机对战入口。
// stdin 协议: move | back | turn N/E/S/W | fire | scan | quit，每行一个动作。
#include "sentry_duel.h"
#include "match.h"

#include <cctype>
#include <csetjmp>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <iostream>
#include <sstream>
#include <string>
#include <sys/time.h>

static sigjmp_buf timeout_jmp;
static void on_sigalrm(int) { siglongjmp(timeout_jmp, 1); }
static void on_sigsegv(int) { siglongjmp(timeout_jmp, 2); }

struct AiState {
    void* handle = nullptr;
    void (*act_fn)(const Board&, char) = nullptr;
    std::string name;
};

static void install_timeout() {
    itimerval timer = {};
    timer.it_value.tv_sec = 1;
    setitimer(ITIMER_REAL, &timer, nullptr);
}

static void clear_timeout() {
    itimerval timer = {};
    setitimer(ITIMER_REAL, &timer, nullptr);
}

static int call_ai_act(AiState& ai, const Board& view, char side) {
    int rc = sigsetjmp(timeout_jmp, 1);
    if (rc != 0) {
        clear_timeout();
        return rc;
    }
    struct sigaction alarm_action = {}, segv_action = {};
    alarm_action.sa_handler = on_sigalrm;
    segv_action.sa_handler = on_sigsegv;
    sigemptyset(&alarm_action.sa_mask);
    sigemptyset(&segv_action.sa_mask);
    sigaction(SIGALRM, &alarm_action, nullptr);
    sigaction(SIGSEGV, &segv_action, nullptr);
    install_timeout();
    try {
        ai.act_fn(view, side);
    } catch (...) {
        clear_timeout();
        return 2;
    }
    clear_timeout();
    return 0;
}

static sentry::Match::Policy make_ai_policy(AiState& ai) {
    return [&ai](const Board& view, char side) {
        return call_ai_act(ai, view, side);
    };
}

static char opposite_facing(char facing) {
    switch (facing) {
        case 'N': return 'S';
        case 'S': return 'N';
        case 'E': return 'W';
        case 'W': return 'E';
        default: return 'S';
    }
}

static sentry::Match::Policy make_human_policy() {
    return [](const Board& board, char side) {
        int consumed_actions = 0;
        for (int attempt = 0; attempt < 32 && consumed_actions < 3; ++attempt) {
            std::string line;
            if (!std::getline(std::cin, line)) return 0;
            std::istringstream input(line);
            std::string command;
            input >> command;
            for (char& c : command) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
            if (command == "quit" || command == "exit") return 2;
            if (command == "end" || command == "pass") return 0;

            if (command == "move") {
                const ActionResult result = move();
                if (result.consumed && ++consumed_actions >= 3) return 0;
                continue;
            }
            if (command == "back") {
                const Sentry& me = side == 'R' ? board.red : board.blue;
                const ActionResult turned = turn(opposite_facing(me.last_known_facing));
                if (turned.success) {
                    const ActionResult moved = move();
                    consumed_actions += (turned.consumed ? 1 : 0) + (moved.consumed ? 1 : 0);
                    if (consumed_actions >= 3) return 0;
                }
                continue;
            }
            if (command == "fire") {
                const ActionResult result = fire();
                if (result.consumed && ++consumed_actions >= 3) return 0;
                continue;
            }
            if (command == "scan") {
                const ScanResult result = scan();
                if (result.consumed && ++consumed_actions >= 3) return 0;
                continue;
            }
            if (command == "turn") {
                char facing = 0;
                input >> facing;
                facing = static_cast<char>(std::toupper(static_cast<unsigned char>(facing)));
                if (facing == 'N' || facing == 'E' || facing == 'S' || facing == 'W') {
                    const ActionResult result = turn(facing);
                    if (result.consumed && ++consumed_actions >= 3) return 0;
                }
            }
        }
        return 0;
    };
}

static AiState load_ai(const std::string& path) {
    AiState ai;
    ai.name = path;
    ai.handle = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!ai.handle) {
        std::fprintf(stderr, "[engine] dlopen %s 失败: %s\n", path.c_str(), dlerror());
        std::exit(1);
    }
    ai.act_fn = reinterpret_cast<void (*)(const Board&, char)>(dlsym(ai.handle, "act"));
    if (!ai.act_fn) {
        std::fprintf(stderr, "[engine] dlsym(act) 失败: %s\n", dlerror());
        std::exit(1);
    }
    return ai;
}

int main(int argc, char** argv) {
    std::string ai_path;
    std::string human_side = "R";
    int max_turns = 20;
    int game_id = 0;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--ai") == 0 && i + 1 < argc) ai_path = argv[++i];
        else if (std::strcmp(argv[i], "--human-side") == 0 && i + 1 < argc) human_side = argv[++i];
        else if (std::strcmp(argv[i], "--max-turns") == 0 && i + 1 < argc) max_turns = std::atoi(argv[++i]);
        else if (std::strcmp(argv[i], "--game-id") == 0 && i + 1 < argc) game_id = std::atoi(argv[++i]);
        else { std::fprintf(stderr, "用法: %s --ai X.so --human-side R|B [--max-turns 20] [--game-id 0]\n", argv[0]); return 1; }
    }
    if (ai_path.empty() || (human_side != "R" && human_side != "B") || max_turns < 1) return 1;

    AiState ai = load_ai(ai_path);
    auto human = make_human_policy();
    auto ai_policy = make_ai_policy(ai);
    sentry::Match match(human_side == "R" ? human : ai_policy,
                        human_side == "B" ? human : ai_policy,
                        human_side == "R" ? "human" : ai_path,
                        human_side == "B" ? "human" : ai_path,
                        max_turns, game_id,
                        [](const std::string& line) {
                            std::printf("%s\n", line.c_str());
                            std::fflush(stdout);
                        });
    match.run();
    dlclose(ai.handle);
    return 0;
}
