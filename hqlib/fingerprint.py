"""代码指纹：判断“自上次通过以来代码有没有变”。

git 项目：HEAD + 工作区改动（含未跟踪文件）的路径/mtime/size。
非 git 项目：遍历源码树（跳过构建产物目录）的路径/mtime/size。
协议文件（STATUS/DECISIONS/ACCEPTANCE）与 .hq/ 不计入。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .common import PROTOCOL_FILES, STATE_DIR

SKIP_DIRS = {
    ".git", STATE_DIR, "node_modules", "target", "build", "dist", "coverage",
    "test-results", ".pnpm-store", "__pycache__", "temp", "tmp", "library", ".DS_Store",
}


def _is_protocol(rel: str) -> bool:
    return rel in PROTOCOL_FILES or rel.startswith(STATE_DIR + "/")


# git 模式下只忽略明确的缓存垃圾（检查命令自己会产生，且常未被 .gitignore 覆盖）；
# build/tmp 之类在别的仓库里可能是受跟踪的源码目录，不能一刀切。
JUNK = {"__pycache__", ".DS_Store", "node_modules", ".pnpm-store", ".pytest_cache", ".mypy_cache"}


def _ignored(rel: str, skip: set = JUNK) -> bool:
    return _is_protocol(rel) or any(p in skip for p in rel.split("/"))


def _git(root: Path, *args) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                             timeout=20, check=True)
        return out.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def _stat_token(root: Path, rel: str) -> str:
    try:
        st = os.stat(root / rel)
        return f"{rel}\0{st.st_mtime_ns}\0{st.st_size}"
    except OSError:
        return f"{rel}\0deleted"


def is_git(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree") is not None


def code_fingerprint(root) -> str:
    root = Path(root)
    h = hashlib.sha256()
    if is_git(root):
        h.update((_git(root, "rev-parse", "HEAD") or "no-head").strip().encode())
        raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
        paths = set()
        entries = raw.split("\0")
        i = 0
        while i < len(entries):
            e = entries[i]
            i += 1
            if len(e) < 4:
                continue
            code, rel = e[:2], e[3:]
            if code[0] in "RC":  # 重命名：下一个条目是原路径
                i += 1
            if not _ignored(rel.rstrip("/")):
                paths.add(rel)
        for rel in sorted(paths):
            h.update(_stat_token(root, rel).encode())
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if _ignored(rel, SKIP_DIRS):
                    continue
                h.update(_stat_token(root, rel).encode())
    return h.hexdigest()[:16]
