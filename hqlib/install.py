"""接入与安装：hq init / hq sync-global / hq doctor。"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from .common import (ACCEPTANCE_FILE, CONFIG_PATH, DECISIONS_FILE, HQ_HOME, PROTOCOL_DIR,
                     STATUS_FILE, TASTE_DIR, load_config, now_str, read_json, write_json,
                     write_text)
from .status import parse_checks, parse_status

# 闸门走 PATH 里的 `hq`（~/Desktop/bin 在 PATH；入口链只动链接不动引用）。
# 特殊布局可用 HQ_GATE_CMD 覆盖。
GATE_CMD = os.environ.get("HQ_GATE_CMD", "hq gate")
GATE_TIMEOUT = 300
GLOBAL_SOURCE = TASTE_DIR / "global.md"
GLOBAL_TARGETS = [Path.home() / ".claude" / "CLAUDE.md", Path.home() / ".codex" / "AGENTS.md"]
GENERATED_MARK = "<!-- henry-hq:generated"


# ------------------------------------------------------------------ hooks

def _has_gate(cfg: dict) -> bool:
    for group in cfg.get("hooks", {}).get("Stop", []):
        for h in group.get("hooks", []):
            if "hq" in h.get("command", "") and h.get("command", "").endswith(" gate"):
                return True
    return False


def install_hook(settings_path: Path) -> bool:
    """在 Claude(.claude/settings.json) 或 Codex(.codex/hooks.json) 配置里追加 Stop 闸门。"""
    cfg = read_json(settings_path, {}) if settings_path.exists() else {}
    if cfg is None:
        raise ValueError(f"{settings_path} 不是合法 JSON，未改动")
    if _has_gate(cfg):
        return False
    stop = cfg.setdefault("hooks", {}).setdefault("Stop", [])
    stop.append({"hooks": [{"type": "command", "command": GATE_CMD, "timeout": GATE_TIMEOUT,
                            "statusMessage": "hq 验收闸门"}]})
    write_json(settings_path, cfg)
    return True


def _ensure_gitignore(root: Path) -> bool:
    gi = root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if any(line.strip() in (".hq", ".hq/", "/.hq/") for line in text.splitlines()):
        return False
    sep = "" if (not text or text.endswith("\n")) else "\n"
    write_text(gi, f"{text}{sep}# henry-hq 机器产物\n.hq/\n")
    return True


def _register(root: Path) -> bool:
    cfg = load_config(CONFIG_PATH)
    if any(Path(p["path"]).resolve() == root for p in cfg["projects"]):
        return False
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else "wip_limit = 3\n"
    sep = "" if text.endswith("\n") else "\n"
    write_text(CONFIG_PATH, f'{text}{sep}\n[[projects]]\npath = "{root}"\n')
    return True


def _render_template(template: str, **kw) -> str:
    text = (PROTOCOL_DIR / template).read_text(encoding="utf-8")
    for k, v in kw.items():
        text = text.replace("{" + k + "}", v)
    return text


def init_project(path, name=None, theme="未分类", value="好玩", hooks=True) -> list:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"目录不存在: {root}")
    name = name or root.name
    done = []
    for fname, tpl in ((STATUS_FILE, "STATUS.template.md"), (ACCEPTANCE_FILE, "ACCEPTANCE.template.md"),
                       (DECISIONS_FILE, "DECISIONS.template.md")):
        target = root / fname
        if target.exists():
            done.append(f"保留已有 {fname}")
            continue
        write_text(target, _render_template(tpl, name=name, theme=theme, value=value, updated=now_str()))
        done.append(f"创建 {fname}")
    if _ensure_gitignore(root):
        done.append("在 .gitignore 加入 .hq/")
    if hooks:
        for p in (root / ".claude" / "settings.json", root / ".codex" / "hooks.json"):
            if install_hook(p):
                done.append(f"安装 Stop 闸门 → {p.relative_to(root)}")
    if _register(root):
        done.append(f"登记到 {CONFIG_PATH.name}")
    return done


# ------------------------------------------------------------ global prompt

def render_global() -> str:
    src = GLOBAL_SOURCE.read_text(encoding="utf-8")
    header = (f"{GENERATED_MARK} 源文件: {GLOBAL_SOURCE} -->\n"
              "<!-- 修改请编辑源文件后运行 `hq sync-global`；直接改这里会在下次同步时被覆盖。 -->\n\n")
    return header + src


def sync_global(dry_run=False) -> list:
    rendered = render_global()
    out = []
    for target in GLOBAL_TARGETS:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current == rendered:
            out.append(f"已同步 {target}")
            continue
        if current.strip() and not current.startswith(GENERATED_MARK):
            backup = HQ_HOME / "backups" / f"{target.parent.name.lstrip('.')}-{target.name}.{datetime.now():%Y%m%d%H%M%S}"
            if not dry_run:
                backup.parent.mkdir(exist_ok=True)
                shutil.copy2(target, backup)
            out.append(f"备份原文件 {target} → {backup}")
        if not dry_run:
            write_text(target, rendered)
        out.append(f"写入 {target}")
    return out


def global_drift() -> list:
    rendered = render_global()
    return [str(t) for t in GLOBAL_TARGETS
            if not t.exists() or t.read_text(encoding="utf-8") != rendered]


# ------------------------------------------------------------------ doctor

def doctor() -> list:
    """返回 [(ok: bool, 描述)]。"""
    rows = []
    cfg = load_config()
    drift = global_drift()
    rows.append((not drift, "全局指令已同步" if not drift else f"全局指令未同步: {', '.join(drift)}（运行 hq sync-global）"))
    for p in cfg["projects"]:
        root = Path(p["path"])
        tag = root.name
        st = parse_status(root / STATUS_FILE)
        rows.append((st is not None, f"{tag}: STATUS.md {'可解析' if st else '缺失'}"))
        try:
            checks = parse_checks(root / ACCEPTANCE_FILE)
            rows.append((bool(checks), f"{tag}: ACCEPTANCE.md {len(checks)} 项检查"
                         f"（quick {sum(c['tier'] == 'quick' for c in checks)}）"))
        except ValueError as e:
            rows.append((False, f"{tag}: {e}"))
        rows.append(((root / DECISIONS_FILE).exists(), f"{tag}: DECISIONS.md"))
        for label, hp in (("Claude", root / ".claude" / "settings.json"), ("Codex", root / ".codex" / "hooks.json")):
            ok = hp.exists() and _has_gate(read_json(hp, {}) or {})
            rows.append((ok, f"{tag}: {label} Stop 闸门{'已安装' if ok else '未安装'}"))
    # launchd 起的进程受 TCC 限制读不了 ~/Desktop，所以早晚报走 Claude App 的定时任务
    tasks = Path.home() / ".claude" / "scheduled-tasks"
    for tid, label in (("hq-morning-brief", "早报"), ("hq-evening-brief", "晚报")):
        ok = (tasks / tid / "SKILL.md").exists()
        rows.append((ok, f"定时{label}（Claude App 定时任务 {tid}）{'已创建' if ok else '缺失'}"))
    tokei = Path.home() / ".tokei" / "usage.30s.py"
    has = tokei.exists() and "hq_status" in tokei.read_text(encoding="utf-8", errors="ignore")
    rows.append((has, f"tokei 采集脚本{'已支持' if has else '尚未支持'}项目状态"))
    return rows
