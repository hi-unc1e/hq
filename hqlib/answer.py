"""hq todo / ok / note：不打开文件就能答复 STATUS.md 里的 ❓。

编号 = 条目在该项目「❓ 待 Henry 判断」小节里的顺序（从 1 开始，含已答复的条目），
所以答复一条不会让其他条目的编号变化；agent 改写 STATUS 后编号才会变，用前先 `hq todo`。
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import STATUS_FILE, load_config, now_str, write_text
from .status import parse_status

_ITEM = re.compile(r"^(\s{0,3}[-*]\s+)(\[[ xX]\]\s+)?(.+?)\s*$")


def _needs_you_lines(lines: list) -> list:
    """返回 ❓ 小节里列表项的行号。"""
    out, inside = [], False
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("## "):
            inside = st[3:].lstrip().startswith("❓")
            continue
        if st.startswith("# "):
            inside = False
            continue
        if inside and _ITEM.match(line) and st[2:].strip() not in ("无", "暂无"):
            out.append(i)
    return out


def _projects() -> dict:
    out = {}
    for p in load_config()["projects"]:
        root = Path(p["path"])
        st = parse_status(root / STATUS_FILE)
        out[(st or {}).get("name", root.name)] = root
    return out


def current_project(cwd=None) -> str | None:
    """cwd 所在的登记项目名（取最深匹配）；不在任何项目里则 None。"""
    from pathlib import Path as _P

    cur = (cwd or _P.cwd()).resolve()
    best_name, best_root = None, None
    for name, root in _projects().items():
        try:
            cur.relative_to(root.resolve())
        except ValueError:
            continue
        if best_root is None or len(root.parts) > len(best_root.parts):
            best_name, best_root = name, root
    return best_name


def todo(scope: str | None = None) -> list:
    """[(id, 状态, 标题)]；状态：待答 / 已通过 / 已批注。

    scope 给定（通常是 current_project() 的结果）时只列该项目；
    None 列全部项目。行/答复逻辑不变。"""
    rows = []
    for name, root in _projects().items():
        if scope is not None and name != scope:
            continue
        path = root / STATUS_FILE
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, i in enumerate(_needs_you_lines(lines), 1):
            m = _ITEM.match(lines[i])
            text = m.group(3)
            if m.group(2) and m.group(2).strip().lower() == "[x]":
                state = "已通过"
            elif re.search(r"(?:→|->)\s*Henry\s*[:：]\s*(?![…\s`]|\.\.\.)\S", text):
                state = "已批注"
            else:
                state = "待答"
            title = re.sub(r"\*\*(.+?)\*\*", r"\1", text.split(" — ")[0])
            rows.append((f"{name}#{n}", state, title))
    return rows


def item_detail(item_id: str) -> dict:
    """一条 ❓ 的完整详情：全文（含 怎么验/预期/建议）+ 项目上下文。

    todo 列表只显示标题（截到第一个 " — "）；判断要不要通过需要全文。
    """
    path, lines, i = _locate(item_id)
    m = _ITEM.match(lines[i])
    text = m.group(3)
    if m.group(2) and m.group(2).strip().lower() == "[x]":
        state = "已通过"
    elif re.search(r"(?:→|->)\s*Henry\s*[:：]\s*(?![…\s`]|\.)\S", text):
        state = "已批注"
    else:
        state = "待答"
    from .status import parse_status

    st = parse_status(path) or {}
    return {
        "id": item_id,
        "state": state,
        "text": re.sub(r"\*\*(.+?)\*\*", r"\1", text),
        "path": str(path),
        "line_no": i + 1,
        "project": st.get("name", ""),
        "theme": st.get("theme", ""),
        "value": st.get("value", ""),
        "summary": st.get("summary", ""),
        "updated": st.get("updated", ""),
    }


_LABELS = ("怎么验", "预期", "建议")


_NOTE_MARK = re.compile(r"\s*(?:→|->)\s*Henry\s*[:：]\s*(.+)$")


def render_detail(d: dict) -> str:
    """终端友好的详情卡：标题行 + 分行的 怎么验/预期/建议 + 项目上下文 + 答复提示。

    条目约定用 " — " 分段，但也有 "。怎么验：" 句号写法——先归一再分段；
    已有 Henry 批注（→ Henry: …）单独提出来，不和 建议 混排。
    """
    text = d["text"]
    note_m = _NOTE_MARK.search(text)
    note = note_m.group(1).strip() if note_m else None
    if note_m:
        text = text[: note_m.start()]
    for lb in _LABELS:
        text = text.replace(f"。{lb}", f" — {lb}").replace(f"；{lb}", f" — {lb}")
    segs = text.split(" — ")
    out = [f"❓ {d['id']} · {segs[0].strip()}   [{d['state']}]"]
    ctx = " · ".join(x for x in (
        f"{d['project']}（{d['theme']} · {d['value']}）" if d["project"] else "",
        f"更新 {d['updated']}" if d["updated"] else "") if x)
    if ctx:
        out.append(f"   {ctx}")
    if d["summary"]:
        out.append(f"   现状: {d['summary']}")
    out.append("")
    for seg in segs[1:]:
        seg = seg.strip()
        if not seg:
            continue
        hit = next((lb for lb in _LABELS if seg.startswith(lb)), None)
        out.append(f"   {seg}" if hit is None else f"   {hit}：{seg[len(hit) + 1:].lstrip('：: ')}")
    if len(segs) == 1:
        out.append(f"   {segs[0].strip()}")
    if note:
        out.append(f"   你的批注: {note}")
    out += [
        "",
        f"   文件: {d['path']}（❓ 第 {d['line_no']} 行附近）",
        '   答复: hq ok {n}   或   hq note {n} "你的意见"'.format(
            n=d["id"].rsplit("#", 1)[1]),
    ]
    return "\n".join(out)


def _locate(item_id: str):
    if "#" not in item_id:
        raise SystemExit(f"编号格式应为 项目#序号，例如 demo#2（收到 {item_id!r}）")
    name, num = item_id.rsplit("#", 1)
    projects = _projects()
    root = projects.get(name) or next((r for k, r in projects.items() if k.lower() == name.lower()), None)
    if root is None:
        raise SystemExit(f"未登记的项目: {name}（已登记: {', '.join(projects)}）")
    path = root / STATUS_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _needs_you_lines(lines)
    try:
        k = int(num)
        line_no = idx[k - 1]
        if k < 1:
            raise IndexError
    except (ValueError, IndexError):
        raise SystemExit(f"{name} 的 ❓ 里没有第 {num} 条（共 {len(idx)} 条），先运行 hq todo")
    return path, lines, line_no


def _touch_updated(lines: list) -> None:
    for i, line in enumerate(lines[:12]):
        if line.startswith("updated:"):
            lines[i] = f"updated: {now_str()}"
            return


def answer(item_id: str, note: str | None = None) -> str:
    """note 为 None → 勾选通过；否则在条目末尾追加 “→ Henry: note”。"""
    path, lines, i = _locate(item_id)
    m = _ITEM.match(lines[i])
    prefix, text = m.group(1), m.group(3)
    if note is None:
        lines[i] = f"{prefix}[x] {text}"
    else:
        note = note.strip()
        if not note:
            raise SystemExit("批注不能为空")
        box = m.group(2) or "[ ] "
        lines[i] = f"{prefix}{box}{text} → Henry: {note}"
    _touch_updated(lines)
    write_text(path, "\n".join(lines) + "\n")
    return lines[i].strip()
