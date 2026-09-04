# engine/tests/Makefile - 构建确定性测试 AI
# 编译选项与 ai/Makefile 一致:move/turn/fire/scan 在 dlopen 时由引擎进程全局符号表解析。

CXX      ?= g++
CXXFLAGS  = -std=c++17 -O2 -fPIC -Wall -Wextra -MMD -MP -I../include
LDFLAGS   = -shared -Wl,-z,lazy -Wl,--allow-shlib-undefined

TARGETS = det_ai_a.so det_ai_b.so det_ai_sleep.so det_ai_crash.so

all: $(TARGETS)

%.so: %.cpp
	$(CXX) $(CXXFLAGS) $(LDFLAGS) $< -o $@

clean:
	rm -f *.so *.d

.PHONY: all clean

-include $(TARGETS:.so=.d)
