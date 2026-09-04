CXX      ?= g++
CXXFLAGS  = -std=c++17 -O2 -fPIC -Wall -Wextra -MMD -MP -I../engine/include
# 选手 .so 中的 move/turn/fire/scan 在 dlopen 时由 runner 提供(进程全局符号表)
# 用 -z lazy + --allow-shlib-undefined 让链接器不报错
LDFLAGS  = -shared -Wl,-z,lazy -Wl,--allow-shlib-undefined

TARGETS = baseline_ai.so hunter_ai.so

all: $(TARGETS)

baseline_ai.so: baseline_ai.cpp
	$(CXX) $(CXXFLAGS) $(LDFLAGS) baseline_ai.cpp -o baseline_ai.so

hunter_ai.so: hunter_ai.cpp
	$(CXX) $(CXXFLAGS) $(LDFLAGS) hunter_ai.cpp -o hunter_ai.so

clean:
	rm -f *.so *.d

.PHONY: all clean

-include $(TARGETS:.so=.d)
