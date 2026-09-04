"""upload.py - 选手代码上传 + 编译(支持按 author 分目录)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from store import UPLOADS_DIR, DATA_DIR

SERVER_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SERVER_DIR.parent
ENGINE_INCLUDE = PROJECT_DIR / "engine" / "include"
AI_INCLUDE = PROJECT_DIR / "ai"
ENGINE_LIB_DIR = PROJECT_DIR / "engine" / "build"

# 编译参数:选手 .so 链引擎库,允许未定义符号(链接时 -z lazy)
COMPILE_CMD = [
    "g++", "-std=c++17", "-O2", "-fPIC", "-shared",
    "-Wl,-z,lazy", "-Wl,--allow-shlib-undefined",
    f"-I{ENGINE_INCLUDE}",
    f"-I{AI_INCLUDE}",      # 让选手可以 #include "navigation.h" 等公用头
    f"-L{ENGINE_LIB_DIR}",
    "-Wl,-rpath,'$ORIGIN/../engine/build'",
    "-lsentry_duel_engine",
]


def sanitize_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("选手名为空")
    if any(c in name for c in r"/\:*?\"<>|"):
        raise ValueError(f"选手名包含非法字符: {name}")
    if len(name) > 64:
        raise ValueError("选手名过长(>64)")
    return name


def sanitize_author(author: str) -> str:
    """author 用 UUID 或 'name#1234' 这种;禁止路径分隔符。"""
    author = author.strip()
    if not author:
        raise ValueError("作者为空")
    if any(c in author for c in r"/\:*?\"<>|"):
        raise ValueError(f"作者名包含非法字符: {author}")
    if len(author) > 64:
        raise ValueError("作者名过长(>64)")
    return author


def compile_source(src_text: str, author: str, name: str) -> tuple[Path, Path]:
    """编译选手源码到 DATA_DIR/uploads/<author>/<name>.{cpp,so}

    返回 (cpp_path, so_path)。
    失败抛 RuntimeError,带 stderr。
    """
    user_dir = UPLOADS_DIR / author
    user_dir.mkdir(parents=True, exist_ok=True)

    src = user_dir / f"{name}.cpp"
    so = user_dir / f"{name}.so"
    src.write_text(src_text)

    cmd = COMPILE_CMD + [str(src), "-o", str(so)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_DIR))
    if proc.returncode != 0:
        raise RuntimeError(f"编译失败:\n{proc.stderr}\n{proc.stdout}")
    if not so.exists():
        raise RuntimeError(f"编译未产出 .so: {proc.stderr}")
    return src, so


# ---------------- 多文件源码包(zip/tar.gz) ----------------

PACK_MAX_ARCHIVE = 20 * 1024 * 1024  # 压缩包 ≤ 20MB
PACK_MAX_FILES = 64                  # 文件数 ≤ 64
PACK_MAX_TOTAL = 20 * 1024 * 1024    # 解压后总大小 ≤ 20MB
# 导出的 RL 权重头文件通常约 10MB(文本 float 数组),允许其作为源码包成员上传。
PACK_MAX_MEMBER = 12 * 1024 * 1024   # 单文件 ≤ 12MB


def _safe_member_name(raw: str) -> str:
    """校验压缩包内成员路径:拒绝绝对路径/../~ 等,返回规整后的相对路径。"""
    name = raw.replace("\\", "/").strip()
    if not name or name.endswith("/"):
        return ""  # 目录项,跳过
    p = Path(name)
    if p.is_absolute() or name.startswith("~"):
        raise RuntimeError(f"压缩包含非法路径: {raw}")
    parts = [x for x in p.parts if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise RuntimeError(f"压缩包含越界路径: {raw}")
    return "/".join(parts)


def _extract_archive(data: bytes, filename: str, dest: Path) -> None:
    """把 zip/tar(.gz) 安全解压到 dest(逐成员手写,不走 extractall)。"""
    import io
    import tarfile
    import zipfile

    lower = filename.lower()
    members: list[tuple[str, bytes]] = []
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                # 拒绝符号链接(unix 外部属性高 16 位是模式)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError(f"压缩包含符号链接: {info.filename}")
                rel = _safe_member_name(info.filename)
                if not rel:
                    continue
                if info.file_size > PACK_MAX_MEMBER:
                    raise RuntimeError(f"成员过大: {info.filename}")
                members.append((rel, zf.read(info)))
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                if m.issym() or m.islnk():
                    raise RuntimeError(f"压缩包含链接: {m.name}")
                if not m.isfile():
                    continue
                rel = _safe_member_name(m.name)
                if not rel:
                    continue
                if m.size > PACK_MAX_MEMBER:
                    raise RuntimeError(f"成员过大: {m.name}")
                f = tf.extractfile(m)
                members.append((rel, f.read() if f else b""))
    else:
        raise RuntimeError("仅支持 .zip / .tar.gz / .tgz / .tar")

    if not members:
        raise RuntimeError("压缩包里没有文件")
    if len(members) > PACK_MAX_FILES:
        raise RuntimeError(f"文件数超过 {PACK_MAX_FILES}")
    if sum(len(c) for _, c in members) > PACK_MAX_TOTAL:
        raise RuntimeError("解压后总大小超限")

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel, content in members:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)


def _find_project_root(pkg_dir: Path) -> Path:
    """定位项目根:优先包根,否则下钻最多两层找含 Makefile 或 .cpp 的目录
    (兼容 zip 常见的"外层多套一层目录"结构)。"""
    if (pkg_dir / "Makefile").exists() or list(pkg_dir.glob("*.cpp")):
        return pkg_dir
    candidates = [d for d in pkg_dir.iterdir() if d.is_dir()]
    for _ in range(2):
        hits = [d for d in candidates
                if (d / "Makefile").exists() or list(d.glob("*.cpp"))]
        if hits:
            return hits[0]
        candidates = [sub for d in candidates for sub in d.iterdir() if sub.is_dir()]
    return pkg_dir


def compile_package(data: bytes, filename: str, author: str, name: str) -> tuple[Path, Path]:
    """编译多文件源码包到 DATA_DIR/uploads/<author>/<name>.so。

    源码解到 <author>/pkg_<name>/;构建策略:项目根(自动定位)有 Makefile
    则用 make(环境变量 ENGINE_INCLUDE/ENGINE_LIB_DIR 可用),需产出至少一个
    .so;否则用标准命令编译项目根全部 .cpp。
    返回 (主源码路径, so_path)。失败抛 RuntimeError,带 stderr。
    """
    if len(data) > PACK_MAX_ARCHIVE:
        raise RuntimeError("压缩包超过 20MB")
    user_dir = UPLOADS_DIR / author
    user_dir.mkdir(parents=True, exist_ok=True)
    so_path = user_dir / f"{name}.so"
    pkg_dir = user_dir / f"pkg_{name}"
    _extract_archive(data, filename, pkg_dir)
    root = _find_project_root(pkg_dir)

    try:
        if (root / "Makefile").exists():
            env = os.environ.copy()
            # 给选手 Makefile 暴露引擎路径(可选使用)
            env["ENGINE_INCLUDE"] = str(ENGINE_INCLUDE)
            env["ENGINE_LIB_DIR"] = str(ENGINE_LIB_DIR)
            proc = subprocess.run(["make", "-C", str(root)],
                                  capture_output=True, text=True, env=env,
                                  timeout=120)
            built = sorted(root.glob("*.so"), key=lambda p: p.stat().st_mtime)
            if proc.returncode != 0 or not built:
                raise RuntimeError(
                    f"make 失败或未产出 .so:\n{proc.stderr}\n{proc.stdout}")
            shutil.copyfile(built[-1], so_path)
        else:
            srcs = sorted(root.glob("*.cpp"))
            if not srcs:
                raise RuntimeError("包内没有 Makefile,也没有 .cpp 文件")
            cmd = COMPILE_CMD + [str(s) for s in srcs] + [f"-I{root}",
                                                          "-o", str(so_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=str(root), timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"编译失败:\n{proc.stderr}\n{proc.stdout}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("编译超时(120s)")

    if not so_path.exists():
        raise RuntimeError("编译未产出 .so")
    # 主源码路径:优先 my_ai.cpp,否则项目根第一个 .cpp
    mains = sorted(root.glob("*.cpp"))
    main_cpp = root / "my_ai.cpp"
    if not main_cpp.exists() and mains:
        main_cpp = mains[0]
    return main_cpp, so_path
