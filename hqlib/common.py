"""Paths, config and small shared helpers."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - bin/hq re-execs with a newer python
    tomllib = None

HQ_HOME = Path(os.environ.get("HQ_HOME") or Path(__file__).resolve().parent.parent)
CONFIG_PATH = HQ_HOME / "hq.toml"
PROTOCOL_DIR = HQ_HOME / "protocol"
TASTE_DIR = HQ_HOME / "taste"
BRIEFS_DIR = HQ_HOME / "briefs"

STATUS_FILE = "STATUS.md"
ACCEPTANCE_FILE = "ACCEPTANCE.md"
DECISIONS_FILE = "DECISIONS.md"
STATE_DIR = ".hq"
# 这些文件由协议/机器维护，不算“代码改动”。
PROTOCOL_FILES = {STATUS_FILE, DECISIONS_FILE, ACCEPTANCE_FILE}


def load_config(path: Path = None) -> dict:
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        return {"wip_limit": 3, "projects": []}
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("wip_limit", 3)
    cfg.setdefault("projects", [])
    for p in cfg["projects"]:
        p["path"] = os.path.expanduser(p["path"])
    return cfg


def project_paths(cfg: dict = None) -> list:
    cfg = cfg or load_config()
    return [Path(p["path"]) for p in cfg.get("projects", [])]


def find_project_root(start) -> Path | None:
    """向上查找含 ACCEPTANCE.md 的目录（协议接入的项目根）。"""
    cur = Path(start).resolve()
    for d in [cur, *cur.parents]:
        if (d / ACCEPTANCE_FILE).is_file():
            return d
        if d == Path.home() or d == d.parent:
            break
    return None


def state_dir(root: Path) -> Path:
    d = Path(root) / STATE_DIR
    d.mkdir(exist_ok=True)
    return d


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, text: str) -> None:
    """原子写：避免 agent 与 hq 同时写时读到半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)
