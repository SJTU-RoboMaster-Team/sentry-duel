"""pytest 公共配置:在导入任何 server 模块前把 DATA_DIR 指到临时目录。

store.py 在 import 时就会创建 DATA_DIR/uploads/replays,因此环境变量
必须在 conftest 顶部、任何 server 导入之前设置。
"""
import os
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).parent.parent.resolve()
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sd_test_data_")
sys.path.insert(0, str(SERVER_DIR))

import store  # noqa: E402

store.init_db()

# ---- 测试样例源码 ----

MINIMAL_AI = """\
#include "sentry_duel.h"
extern "C" void act(const Board& board, char my_color) {
    (void)board;
    (void)my_color;
}
"""

# 多文件版本: my_ai.cpp 依赖 helper.h / helper.cpp
HELPER_H = """\
#pragma once
int pick_action();
"""

HELPER_CPP = """\
#include "helper.h"
int pick_action() { return 42; }
"""

MY_AI_USES_HELPER = """\
#include "sentry_duel.h"
#include "helper.h"
extern "C" void act(const Board& board, char my_color) {
    (void)board;
    (void)my_color;
    (void)pick_action();
}
"""

BROKEN_AI = """\
#include "sentry_duel.h"
extern "C" void act(const Board& board, char my_color) {
    this is not valid C++ at all;;;
}
"""

MAKEFILE_OK = """\
all:
\tg++ -std=c++17 -O2 -fPIC -shared -I$(ENGINE_INCLUDE) my_ai.cpp -o my_ai.so
"""

MAKEFILE_FAIL = """\
all:
\texit 1
"""

MAKEFILE_NO_SO = """\
all:
\t@echo done
"""


def make_zip(files: dict[str, bytes]) -> bytes:
    """从 {路径: 内容} 构造内存 zip。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def make_targz(files: dict[str, bytes]) -> bytes:
    """从 {路径: 内容} 构造内存 tar.gz。"""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()
