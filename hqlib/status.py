"""STATUS.md / ACCEPTANCE.md 的解析与机器区改写。

STATUS.md 结构约定见 protocol/PROTOCOL.md。解析刻意宽松：只认 front matter、
第一条引用行，以及以 ❓ ⛔ ▶ ✅ 开头的二级标题下的列表项。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

VERIFY_START = "<!-- hq:verify:start -->"
VERIFY_END = "<!-- hq:verify:end -->"

SECTION_KEYS = {"❓": "needs_you", "⛔": "blocked", "▶": "next", "✅": "verified", "🟡": "sampled"}
_EMPTY_ITEMS = {"无", "暂无", "none", "(无)", "（无）", "-"}
# 批注：“→ Henry:” 后面跟着真实内容（示例里的 “→ Henry: …” 不算）。
_ANNOTATION_RE = re.compile(r"(?:→|->)\s*Henry\s*[:：]\s*(?![…\s`]|\.\.\.)\S")
_ITEM_RE = re.compile(r"^\s{0,3}[-*]\s+(?:\[(?P<mark>[ xX])\]\s+)?(?P<text>.+?)\s*$")


def split_front_matter(text: str):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            meta[k.strip()] = v
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def _strip_verify_block(text: str) -> str:
    s, e = text.find(VERIFY_START), text.find(VERIFY_END)
    if s >= 0 and e > s:
        return text[:s] + text[e + len(VERIFY_END):]
    return text


def human_hash(text: str) -> str:
    """STATUS 中 agent/Henry 可写部分的指纹（排除机器区与 updated 字段之外的空白差异）。"""
    body = _strip_verify_block(text)
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def parse_status(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    meta, body = split_front_matter(text)
    body_wo_machine = _strip_verify_block(body)
    summary = ""
    sections = {v: [] for v in SECTION_KEYS.values()}
    current = None
    for line in body_wo_machine.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            current = next((key for emoji, key in SECTION_KEYS.items() if title.startswith(emoji)), None)
            continue
        if stripped.startswith("# "):
            current = None
            continue
        if not summary and stripped.startswith(">"):
            summary = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped.lstrip(">").strip())
            continue
        if current is None:
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        item_text = m.group("text").strip()
        if item_text.lower() in _EMPTY_ITEMS:
            continue
        mark = m.group("mark")
        sections[current].append({
            "text": item_text,
            "checked": bool(mark and mark.lower() == "x"),
            "annotated": bool(_ANNOTATION_RE.search(item_text)),
        })
    pending = [i for i in sections["needs_you"] if not i["checked"] and not i["annotated"]]
    answered = [i for i in sections["needs_you"] if i["checked"] or i["annotated"]]
    return {
        "path": str(path),
        "meta": meta,
        "name": meta.get("project") or path.parent.name,
        "theme": meta.get("theme", ""),
        "value": meta.get("value", ""),
        "state": meta.get("state", "active"),
        "updated": meta.get("updated", ""),
        "summary": summary,
        "sections": sections,
        "needs_you": pending,
        "answered": answered,
        "blocked": sections["blocked"],
        "next": sections["next"],
        "human_hash": human_hash(text),
        "mtime": path.stat().st_mtime,
    }


def replace_verify_block(text: str, block: str) -> str:
    new = f"{VERIFY_START}\n{block.rstrip()}\n{VERIFY_END}"
    s, e = text.find(VERIFY_START), text.find(VERIFY_END)
    if s >= 0 and e > s:
        return text[:s] + new + text[e + len(VERIFY_END):]
    sep = "" if text.endswith("\n") else "\n"
    return f"{text}{sep}\n## ✅ 机器验收\n\n{new}\n"


# ---------------------------------------------------------------- ACCEPTANCE

_CHECKS_FENCE = re.compile(r"```hq-checks\s*\n(.*?)```", re.S)
TIERS = ("quick", "full")


def parse_checks(path: Path) -> list:
    """解析 ```hq-checks 代码块：`tier id [timeout=N] command...`。"""
    path = Path(path)
    if not path.is_file():
        return []
    checks = []
    for block in _CHECKS_FENCE.findall(path.read_text(encoding="utf-8")):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 3 or parts[0] not in TIERS:
                raise ValueError(f"{path}: 无法解析检查行: {raw!r}")
            tier, cid, rest = parts
            timeout = 120 if tier == "quick" else 1800
            m = re.match(r"timeout=(\d+)\s+(.*)", rest)
            if m:
                timeout, rest = int(m.group(1)), m.group(2)
            checks.append({"tier": tier, "id": cid, "timeout": timeout, "cmd": rest.strip()})
    ids = [c["id"] for c in checks]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"{path}: 检查名重复: {sorted(dup)}")
    return checks


def select_checks(checks: list, tier: str) -> list:
    if tier == "quick":
        return [c for c in checks if c["tier"] == "quick"]
    return list(checks)

