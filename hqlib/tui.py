"""hq todo -i — 交互式看板（curses，零依赖）。

动线：列表（↑↓/jk 选择 · Enter 详情 · a 含已答 · q 退出）
   → 详情（y 同意 · n 填写建议 · b/Esc 返回 · q 退出）
   → 答复走 answer.answer() 同一条通路，答完回列表并刷新。

只做薄薄一层视图：数据来自 answer.todo()/item_detail()，写回用 answer.answer()，
与 hq ok / hq note / 简报勾选完全一致——没有第二套语义。
"""

from __future__ import annotations

import curses
import textwrap

from . import answer

LIST_KEYS_HELP = "↑/↓ 或 j/k 选择 · Enter 详情 · a 含已答 · q 退出"
DETAIL_KEYS_HELP = "y 同意 · n 填写建议 · b/Esc 返回 · q 退出"


def _curs_set(visibility: int) -> None:
    """curs_set 在哑终端上会抛 ERR——只是光标显隐，吞掉即可。"""
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


def _rows(scope: str | None, include_answered: bool) -> list:
    rows = answer.todo(scope=scope)
    if not include_answered:
        rows = [r for r in rows if r[1] == "待答"]
    return rows


def _read_line(scr, y: int, prompt: str) -> str | None:
    """底部行编辑器：Enter 返回文本，Esc 返回 None。支持中文（UTF-8 逐字节累积）。"""
    _curs_set(1)
    buf = b""
    try:
        while True:
            scr.move(y, 0)
            scr.clrtoeol()
            scr.addstr(y, 0, prompt + buf.decode("utf-8", "replace"))
            scr.refresh()
            ch = scr.getch()
            if ch in (10, 13):  # Enter
                text = buf.decode("utf-8", "replace").strip()
                return text or None
            if ch == 27:  # Esc
                return None
            if ch in (8, 127, curses.KEY_BACKSPACE):  # Backspace
                # UTF-8 尾字节：0b10xxxxxx 是续字节，退到多字节序列头
                while buf and (buf[-1] & 0xC0) == 0x80:
                    buf = buf[:-1]
                buf = buf[:-1]
                continue
            if 32 <= ch <= 255:
                buf += bytes([ch])  # UTF-8 输入逐字节到达，累积到可解码为止
    finally:
        _curs_set(0)


def _draw_list(scr, rows, sel, scope_label, include_answered, status_msg):
    scr.erase()
    h, w = scr.getmaxyx()
    title = f" hq 待你判断 · {scope_label}"[: w - 1]
    scr.addstr(0, 0, title, curses.A_BOLD)
    scr.addstr(1, 0, "─" * (w - 1), curses.A_DIM)
    top = 3
    visible = h - top - 2
    if not rows:
        scr.addstr(top, 0, "没有待你判断的条目。")
    start = max(0, min(sel - visible + 1, len(rows) - visible)) if rows else 0
    for i, (rid, state, title_text) in enumerate(
            rows[start:start + max(1, visible)], start):
        mark = {"待答": "❓", "已通过": "✅", "已批注": "💬"}[state]
        line = f" {mark} {rid:<22} {title_text}"[: w - 1]
        attr = curses.A_REVERSE if i == sel else (
            curses.A_DIM if state != "待答" else curses.A_NORMAL)
        scr.addstr(top + (i - start), 0, line, attr)
    if status_msg:
        scr.addstr(h - 2, 0, status_msg[: w - 1], curses.A_DIM)
    scr.addstr(h - 1, 0, LIST_KEYS_HELP[: w - 1], curses.A_DIM)


def _draw_detail(scr, rid: str, width: int):
    scr.erase()
    d = answer.item_detail(rid)
    lines: list[tuple[str, int]] = []
    head = f" ❓ {d['id']} · {d['text'].split(' — ')[0].split('。')[0][:40]}"
    lines.append((head + f"   [{d['state']}]", curses.A_BOLD))
    if d["project"]:
        lines.append((f"   {d['project']}（{d['theme']} · {d['value']}）"
                      f" · 更新 {d['updated']}", curses.A_DIM))
    if d["summary"]:
        lines.append((f"   现状: {d['summary']}", curses.A_DIM))
    lines.append(("", 0))
    body = d["text"]
    for lb in ("怎么验", "预期", "建议"):
        body = body.replace(f"。{lb}", f" — {lb}")
    segs = body.split(" — ")
    lines.append(("   " + segs[0].strip(), curses.A_NORMAL))
    for seg in segs[1:]:
        hit = next((x for x in ("怎么验", "预期", "建议") if seg.strip().startswith(x)), None)
        text = (f"   {seg.strip()}" if hit is None
                else f"   {hit}：{seg.strip()[len(hit) + 1:].lstrip('：: ')}")
        lines.append((text, curses.A_NORMAL))
    lines.append(("", 0))
    lines.append(("   y 同意 · n 填写建议 · b 返回 · q 退出", curses.A_DIM))

    h, w = scr.getmaxyx()
    y = 0
    for text, attr in lines:
        for wrapped in textwrap.wrap(text, width=max(20, w - 2)) or [""]:
            if y < h - 1:
                scr.addstr(y, 0, wrapped[: w - 1], attr)
                y += 1
    scr.refresh()
    return d


def run_tui(scope: str | None = None, include_answered: bool = False) -> int:
    scope_label = scope or "全部项目"
    return curses.wrapper(_main, scope, scope_label, include_answered)


def _main(scr, scope, scope_label, include_answered) -> int:
    _curs_set(0)
    scr.keypad(True)
    rows = _rows(scope, include_answered)
    sel, status_msg = 0, ""
    while True:
        _draw_list(scr, rows, sel, scope_label, include_answered, status_msg)
        ch = scr.getch()
        status_msg = ""
        if ch in (ord("q"), ord("Q")):
            return 0
        if ch in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = min(max(0, len(rows) - 1), sel + 1)
        elif ch in (ord("a"), ord("A")):
            include_answered = not include_answered
            rows = _rows(scope, include_answered)
            sel = min(sel, max(0, len(rows) - 1))
            status_msg = "已切换：" + ("含已答复" if include_answered else "只看待答")
        elif ch in (10, 13) and rows:  # Enter → 详情
            rid = rows[sel][0]
            verdict = _detail_loop(scr, rid)
            if verdict == "quit":
                return 0
            if verdict:
                status_msg = verdict
            rows = _rows(scope, include_answered)
            sel = min(sel, max(0, len(rows) - 1))


def _detail_loop(scr, rid: str) -> str | None:
    """详情层：返回 None(返回) / 'quit' / 状态提示文本。"""
    h, w = scr.getmaxyx()
    while True:
        _draw_detail(scr, rid, w)
        ch = scr.getch()
        if ch in (ord("q"), ord("Q")):
            return "quit"
        if ch in (ord("b"), ord("B"), 27):
            return None
        if ch in (ord("y"), ord("Y")):
            answer.answer(rid)
            return f"已通过 {rid}"
        if ch in (ord("n"), ord("N")):
            note = _read_line(scr, h - 3, " 建议: ")
            if note:
                answer.answer(rid, note)
                return f"已批注 {rid}"
