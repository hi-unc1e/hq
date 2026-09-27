"""hq 命令行入口。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
from pathlib import Path

from . import brief, gate, install
from .common import STATUS_FILE, find_project_root, load_config
from .fingerprint import code_fingerprint
from .status import parse_status
from .verify import load_verify, run_verify, summarize


def _roots(args) -> list:
    if getattr(args, "all", False):
        return [Path(p["path"]) for p in load_config()["projects"]]
    root = find_project_root(args.path or os.getcwd())
    if root is None:
        sys.exit(f"找不到 ACCEPTANCE.md（{args.path or os.getcwd()} 未接入 henry-hq，先 `hq init`）")
    return [root]


def _busy(root: Path, minutes: int) -> bool:
    """最近 N 分钟内有文件改动 → 可能有 agent 正在干活，夜间任务跳过。"""
    cutoff = time.time() - minutes * 60
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".hq", "node_modules", "target", "build",
                                                        "test-results", "temp", "library", "coverage"}]
        for f in filenames:
            try:
                if os.stat(os.path.join(dirpath, f)).st_mtime > cutoff:
                    return True
            except OSError:
                pass
    return False


def cmd_verify(args) -> int:
    rc = 0
    for root in _roots(args):
        if args.skip_busy and _busy(root, args.skip_busy):
            print(f"⏭  {root.name}: 最近 {args.skip_busy} 分钟有改动，跳过")
            continue
        print(f"== {root.name} ({args.tier})")
        res = run_verify(root, args.tier)
        n_ok = sum(r["ok"] for r in res["results"].values())
        n_skip = sum(r["not_run"] for r in res["results"].values())
        skip = f"（{n_skip} 项未运行）" if n_skip else ""
        print(f"{'✅' if res['ok'] else '❌'} {root.name}: {n_ok}/{len(res['results'])} 通过{skip}")
        rc = rc or (0 if res["ok"] else 1)
    return rc


def cmd_status(args) -> int:
    out = []
    for p in load_config()["projects"]:
        root = Path(p["path"])
        st = parse_status(root / STATUS_FILE)
        ver = summarize(load_verify(root), code_fingerprint(root))
        out.append({"path": str(root), "name": (st or {}).get("name", root.name),
                    "state": (st or {}).get("state"), "theme": (st or {}).get("theme"),
                    "needs_you": len((st or {}).get("needs_you", [])),
                    "blocked": len((st or {}).get("blocked", [])),
                    "verify": ver, "summary": (st or {}).get("summary", "")})
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for r in out:
        v = r["verify"]
        print(f"{r['name']:<14} {r['state'] or '未接入':<7} ❓{r['needs_you']} ⛔{r['blocked']} "
              f"✅{v['passed']}/{v['total']}{' ⚠' if v['stale'] or v['failed'] else ''}  {r['summary'][:60]}")
    return 0


def _open_in_viewer(path) -> bool:
    """Typora（有则用，无则系统默认 app）。成功 True。"""
    import subprocess

    for target in ("-a", "Typora"), ():
        cmd = ["open", *target, str(path)] if target else ["open", str(path)]
        try:
            subprocess.run(cmd, check=True, timeout=15)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def cmd_brief(args) -> int:
    if args.sync:
        applied, skipped = brief.sync_from_brief(
            pathlib.Path(args.sync) if args.sync is not True else None)
        for rid, note in applied:
            print("✅" if note is None else "💬", rid, note or "")
        for s in skipped:
            print("⏭️ 未匹配（简报可能过期）：", s, file=sys.stderr)
        if not applied:
            print("简报里没有新的勾选。")
        return 0
    text, path = brief.run_brief(args.slot, write=not args.no_write,
                                 do_notify=args.notify, fresh=args.fresh)
    # 手敲 + 已落盘：终端里倒整份 markdown 不如直接用 Typora 看；
    # 要看纯文本用 --cat，机器/定时路径（非 TTY）行为不变。
    if path and sys.stdout.isatty() and not args.cat:
        if _open_in_viewer(path):
            print(f"简报已用 Typora 打开：{path}")
            return 0
    print(text)
    if path:
        print(f"\n（已写入 {path}）", file=sys.stderr)
    return 0


def cmd_init(args) -> int:
    for line in install.init_project(args.path, args.name, args.theme, args.value, hooks=not args.no_hooks):
        print("•", line)
    return 0


def cmd_sync_global(args) -> int:
    for line in install.sync_global(dry_run=args.dry_run):
        print("•", line)
    return 0


def cmd_doctor(args) -> int:
    rows = install.doctor()
    for ok, text in rows:
        print(("✅ " if ok else "❌ ") + text)
    return 0 if all(ok for ok, _ in rows) else 1


def cmd_nightly(args) -> int:
    """夜间：对空闲项目跑 full 验收，然后出简报。"""
    args.all, args.path, args.tier = True, None, "full"
    cmd_verify(args)
    brief.run_brief(args.slot, write=True, do_notify=args.notify)
    return 0


def cmd_scheduled(args) -> int:
    """launchd 入口：清晨跑 full 验收 + 早报，晚上只出晚报。"""
    from datetime import datetime
    if datetime.now().hour < 12:
        ns = argparse.Namespace(slot="am", skip_busy=20, notify=True, all=True, path=None, tier="full")
        return cmd_nightly(ns)
    brief.run_brief("pm", write=True, do_notify=True)
    return 0


def cmd_todo(args) -> int:
    from . import answer
    # -i = 交互式看板（curses）：列表选择 → 详情 → 即答。
    if getattr(args, "interactive", False):
        from .tui import run_tui

        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            print("hq todo -i 需要交互式终端（stdout/stdin 都要在 tty 上）。")
            return 2
        return run_tui(scope=None if args.all else answer.current_project())
    # 带编号 = 详情视图：看这一条的 怎么验/预期/建议 全文（短编号可用）。
    if getattr(args, "item", None):
        print(answer.render_detail(answer.item_detail(_expand_id(args.item))))
        return 0
    # 默认按当前目录锁定项目：在登记项目里只看该项目的 ❓；
    # -a/--all 跳到全项目视角（同时含已答复条目）。
    scope = None if args.all else answer.current_project()
    rows = [r for r in answer.todo() if scope is None
            or r[0].rsplit("#", 1)[0] == scope]
    shown = [r for r in rows if args.all or r[1] == "待答"]
    if not shown:
        hint = "（--all 查看全部项目）" if scope else ""
        print(f"没有待你判断的条目。{hint}")
        return 0
    width = max(len(r[0]) for r in shown)
    for rid, state, title in shown:
        mark = {"待答": "❓", "已通过": "✅", "已批注": "💬"}[state]
        print(f"{mark} {rid:<{width}}  {title}")
    if not args.all:
        done = len(rows) - len(shown)
        hints = ""
        if scope:
            hidden = sum(1 for r in answer.todo() if r[1] == "待答") - len(shown)
            if hidden > 0:
                hints += f"（其他项目还有 {hidden} 条待答，--all 查看）"
        if done:
            hints += f"（另有 {done} 条已答复，--all 查看）"
        print(f"\n答复：hq ok <编号> 通过；hq note <编号> \"意见\" 批注。{hints}")
    return 0


def _expand_id(item_id: str) -> str:
    """裸短编号（`1` 或 `#1`）→ 当前项目#N（在 cmd 层展开：argparse 已把 id 与
    正文分开，note 文本里的数字不会被误伤）。不在登记项目目录里时给出可操作的错误。"""
    if item_id.isdigit():
        item_id = "#" + item_id
    if not (item_id.startswith("#") and item_id[1:].isdigit()):
        return item_id
    from . import answer

    proj = answer.current_project()
    if proj is None:
        raise SystemExit(
            "#N 只在登记项目目录里可用；不在项目里请写全编号，如 hq ok auto-jb-ape#1")
    return f"{proj}{item_id}"


def cmd_ok(args) -> int:
    from . import answer
    for rid in args.ids:
        print("✅", answer.answer(_expand_id(rid)))
    return 0


def cmd_note(args) -> int:
    from . import answer
    rid = _expand_id(args.id)
    if not args.text:
        # 忘写意见是最常见的手滑：先看全文再决定，别让 argparse 报错挡路。
        print(answer.render_detail(answer.item_detail(rid)))
        print('   （补上意见即可：hq note <编号> "…"）')
        return 0
    print("💬", answer.answer(rid, " ".join(args.text)))
    return 0


def cmd_mine(args) -> int:
    from . import mine
    path = mine.run(days=args.days, out_dir=args.out)
    print(path)
    return 0



_HUMAN_CMDS = ["status", "todo", "ok", "note", "brief"]
_MACHINE_CMDS = ["verify", "gate", "init", "sync-global", "doctor", "mine",
                 "nightly", "scheduled"]


def _grouped_help(p, sub):
    """给顶层 --help 的子命令清单插分组标题（人工 / 机器）。

    不碰 argparse 内部结构（3.13 移除了 _ChoicesPseudoAction）：
    对 format_help 的产出做文本插入，按每个分组首个子命令的行定位。
    """

    def render():
        raw = p.format_help.__wrapped__()
        lines = raw.splitlines()
        # 子命令条目区：{…} 行之后到 "options:" 之前；条目 = 4 空格缩进行，续行更深
        # 条目区 = "positional arguments:" 下的 {…} 行到 "options:" 之间
        # （usage 里也有一行 {…}，必须定位段落头，否则会把 description 裁掉）
        pa = next(i for i, ln in enumerate(lines) if ln.strip() == "positional arguments:")
        start = next(i for i in range(pa, len(lines)) if lines[i].lstrip().startswith("{"))
        end = next(i for i, ln in enumerate(lines) if ln.strip() == "options:")
        entries, cur = [], None
        for line in lines[start + 1:end]:
            if line.startswith("    ") and not line.startswith("        "):
                cur = [line]
                entries.append(cur)
            elif cur is not None and line.strip():
                cur.append(line)
        by_name = {}
        for entry in entries:
            name = entry[0].strip().split()[0]
            by_name[name] = entry
        ordered = (["  ── 人工（你手敲） ──"]
                   + [ln for n in _HUMAN_CMDS for ln in by_name[n]]
                   + ["  ── 机器 / agent 侧（hook、定时、skill 用；你不用敲） ──"]
                   + [ln for n in _MACHINE_CMDS for ln in by_name[n]])
        return "\n".join(lines[:start + 1] + ordered + lines[end:])

    import functools

    p.format_help = functools.wraps(p.format_help)(render)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hq", description="henry-hq：统一视图、验收闸门、品味知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("verify", help="跑 ACCEPTANCE.md 的机器检查")
    s.add_argument("path", nargs="?")
    s.add_argument("--tier", choices=["quick", "full"], default="quick")
    s.add_argument("--all", action="store_true", help="所有已登记项目")
    s.add_argument("--skip-busy", type=int, default=0, metavar="分钟", help="最近 N 分钟有改动则跳过")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("gate", help="Stop hook 入口（stdin 读 hook JSON）")
    s.set_defaults(func=lambda a: gate.main())

    s = sub.add_parser("status", help="一行一个项目的现状")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "todo",
        help="列出待你判断的 ❓（默认只看当前目录所在项目）；hq todo 3 看详情，-i 进交互看板")
    s.add_argument("item", nargs="?", help="编号（3 / #3 / 项目#3）：显示该条全文详情")
    s.add_argument("-i", "--interactive", action="store_true",
                   help="交互式看板：上下选择、Enter 看详情并当场答复")
    s.add_argument("-a", "--all", action="store_true",
                   help="全部项目，且含已答复条目")
    s.set_defaults(func=cmd_todo)

    s = sub.add_parser(
        "ok", help="通过一条或多条 ❓：hq ok demo#1；项目目录里可写 hq ok 1")
    s.add_argument("ids", nargs="+")
    s.set_defaults(func=cmd_ok)

    s = sub.add_parser(
        "note", help='批注一条 ❓：hq note demo#1 "太吵了"；项目目录里可写 hq note#1 "太吵了"')
    s.add_argument("id")
    s.add_argument("text", nargs="*", help="省略时显示该条详情（先看再答）")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("brief", help="生成早/晚报（手敲时默认 Typora 打开；--cat 走终端）")
    s.add_argument("--slot", choices=["am", "pm"])
    s.add_argument("--no-write", action="store_true")
    s.add_argument("--notify", action="store_true")
    s.add_argument("--cat", action="store_true", help="终端输出纯文本（默认手敲时用 Typora 打开）")
    s.add_argument("--sync", nargs="?", const=True, metavar="简报.md",
                    help="把简报里的 [x] 勾选/批注写回各项目 STATUS（默认 latest.md）")
    s.add_argument("--fresh", action="store_true", help="跳过 tokei 缓存重新采集")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("nightly", help="空闲项目跑 full 验收 + 出简报（launchd 调用）")
    s.add_argument("--slot", choices=["am", "pm"])
    s.add_argument("--skip-busy", type=int, default=20)
    s.add_argument("--notify", action="store_true")
    s.set_defaults(func=cmd_nightly)

    s = sub.add_parser("scheduled", help="launchd 定时入口（清晨 full 验收+早报，晚上晚报）")
    s.set_defaults(func=cmd_scheduled)

    s = sub.add_parser("init", help="让一个项目接入协议")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--theme", default="未分类")
    s.add_argument("--value", default="好玩", help="收入 / 学习 / 好玩 / 自用 / 研究")
    s.add_argument("--no-hooks", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("sync-global", help="把 taste/global.md 写入 ~/.claude/CLAUDE.md 与 ~/.codex/AGENTS.md")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync_global)

    s = sub.add_parser("doctor", help="检查安装是否完整")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("mine", help="从历史会话里挖你的纠正/偏好，生成品味候选")
    s.add_argument("--days", type=int, default=120)
    s.add_argument("--out")
    s.set_defaults(func=cmd_mine)
    _grouped_help(p, sub)
    return p


def _expand_fused(argv: list) -> list:
    """把融合子命令改写成标准形式：hq ok#1 → hq ok <当前项目>#1。

    只改写子命令位（第一个参数），note 文本里出现 "ok#1" 不会被误伤。
    不在登记项目目录里时给出可操作的错误（写全 项目#N）。
    """
    if not argv:
        return list(argv)
    m = re.fullmatch(r"(ok|note)#(\d+)", str(argv[0]))
    if not m:
        return list(argv)
    from . import answer

    proj = answer.current_project()
    if proj is None:
        raise SystemExit(
            "ok#N 只在登记项目目录里可用；不在项目里请写全编号，如 hq ok auto-jb-ape#1")
    return [m.group(1), f"{proj}#{m.group(2)}"] + list(argv[1:])


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_expand_fused(raw))
    return args.func(args) or 0
