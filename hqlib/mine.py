"""hq mine：从 Codex / Claude Code 历史会话里挖 Henry 的原话，产出品味候选。

只读本机会话日志，不改动任何东西。产出：
  taste/candidates/<日期>.jsonl  全量（已去重），供后续再加工
  taste/candidates/<日期>.md     按项目 × 类别挑出的高信号原话 + 推进型口令统计
候选只是原料：写进 taste/*.md 或项目 DECISIONS.md 之前要经过人（或 agent 起草 + 人审）确认。
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from .common import TASTE_DIR, write_text

HOME = Path.home()

CATEGORIES = {
    "纠正": r"不对|不是这样|错了|为什么|又出现|还是没|没看到|不行|怎么又|没生效|失败了|有 ?bug|有问题",
    "审美": r"丑|好看|美观|风格|颜色|配色|字体|样式|排版|手感|体验|好玩|可爱|高级感|质感|动画|视觉|简洁|啰嗦|太长|看不懂",
    "协作": r"不需要问我|不用问我|自己决定|你决定|直到|验收|自查|自测|不要停|别停|汇报|进度|先.*再|计划|一步步|commit|提交",
    "红线": r"不要|别|禁止|必须|一定要|不能|务必|不允许|密码|权限|泄露|不提交|私有",
    "偏好": r"我希望|我喜欢|我更|我倾向|最好|建议|我想要|我的习惯|以后都|后续.*都|每次都",
}
_CAT_RE = {k: re.compile(v, re.I) for k, v in CATEGORIES.items()}

# 零信息量的推进口令：单独统计，用来证明“哪些话该变成默认授权”
NUDGE_RE = re.compile(
    r"^(go( on)?( please)?|continue|继续.{0,12}|可以.{0,6}|好的?.{0,6}|是的?.{0,6}|yes.{0,10}|ok.{0,6}|"
    r"同意.{0,8}|solve it|go ahead|进度|嗯+|行|对)[。！!.,，\s]*$", re.I)

_SKIP_PREFIXES = ("<", "# AGENTS.md", "--- Orchestration Messages", "你是一位", "You are ", "Caveat:", "[Request interrupted", "This session is being continued",
                  "The following is the Codex agent history", "Base directory for this skill")


# 历史会话里会混有密码/令牌，候选文件落盘前必须脱敏
_SECRET_RES = [
    (re.compile(r"(sshpass\s+-p\s*)(['\"]).*?\2"), r"\1'[已脱敏]'"),
    (re.compile(r"(sshpass\s+-p\s*)\S+"), r"\1[已脱敏]"),
    (re.compile(r"(?i)((?:password|passwd|pwd|密码|token|secret|api[_-]?key|access[_-]?key)\s*[:=：]\s*)(['\"]?)[^\s'\"，,]+\2"),
     r"\1[已脱敏]"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"), "[已脱敏]"),
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b"), "[已脱敏]"),
]


def redact(text: str) -> str:
    for rx, rep in _SECRET_RES:
        text = rx.sub(rep, text)
    return text


def _clean(text: str) -> str | None:
    t = (text or "").strip()
    if not t or t.startswith(_SKIP_PREFIXES):
        return None
    # Codex 附件消息：前面是附件清单，真正的话在 “## My request:” 之后
    m = re.search(r"## My request(?: for Codex)?:", t)
    if m:
        t = t[m.end():].strip()
    # 去掉注入的上下文块（有些客户端把附件/提醒拼在用户消息后面）
    t = re.split(r"\n<(?:system-reminder|environment_context|user_instructions|image)", t)[0].strip()
    return redact(t) or None


def _iter_codex(cutoff: float):
    files = glob.glob(str(HOME / ".codex/sessions/**/*.jsonl"), recursive=True)
    files += glob.glob(str(HOME / ".codex/archived_sessions/**/*.jsonl"), recursive=True)
    for f in files:
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            cwd, sub = "", False
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"session_meta"' in line[:300]:
                        p = json.loads(line).get("payload") or {}
                        cwd = p.get("cwd") or ""
                        sub = isinstance(p.get("source"), dict) and "subagent" in p["source"]
                        if sub:
                            break
                        continue
                    if '"role":"user"' not in line and '"role": "user"' not in line:
                        continue
                    o = json.loads(line)
                    p = o.get("payload") or {}
                    if o.get("type") != "response_item" or p.get("role") != "user":
                        continue
                    for c in p.get("content") or []:
                        t = _clean(c.get("text"))
                        if t:
                            yield {"tool": "codex", "cwd": cwd, "ts": o.get("timestamp", ""), "text": t}
        except (OSError, ValueError):
            continue


def _iter_claude(cutoff: float):
    for f in glob.glob(str(HOME / ".claude/projects/*/*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    o = json.loads(line)
                    if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain"):
                        continue
                    content = (o.get("message") or {}).get("content")
                    texts = [content] if isinstance(content, str) else [
                        c.get("text") for c in (content or []) if isinstance(c, dict) and c.get("type") == "text"]
                    for raw in texts:
                        t = _clean(raw)
                        if t:
                            yield {"tool": "claude", "cwd": o.get("cwd", ""), "ts": o.get("timestamp", ""), "text": t}
        except (OSError, ValueError):
            continue


def classify(text: str) -> list:
    return [k for k, rx in _CAT_RE.items() if rx.search(text)]


def score(item: dict) -> float:
    n = len(item["text"])
    length = 1.0 if 12 <= n <= 400 else (0.4 if n < 12 else 0.6)
    return round(len(item["cats"]) * length + (0.5 if "纠正" in item["cats"] else 0), 2)


def collect(days: int = 120) -> tuple:
    cutoff = time.time() - days * 86400
    seen, items, nudges = set(), [], Counter()
    for it in list(_iter_codex(cutoff)) + list(_iter_claude(cutoff)):
        text = it["text"]
        key = re.sub(r"\s+", "", text)[:120]
        if NUDGE_RE.match(text.strip()):
            nudges[text.strip()[:20].lower()] += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        it["project"] = os.path.basename(it["cwd"].rstrip("/")) or "?"
        it["cats"] = classify(text)
        it["score"] = score(it)
        items.append(it)
    return items, nudges


def render_md(items: list, nudges: Counter, days: int, per_bucket: int = 12) -> str:
    by = defaultdict(list)
    for it in items:
        if it["score"] <= 0:
            continue
        # 每条只归入优先级最高的一个类别，避免同一句话在多个类别里重复
        primary = next(c for c in CATEGORIES if c in it["cats"])
        by[(it["project"], primary)].append(it)
    projects = Counter(it["project"] for it in items if it["score"] > 0)
    L = [f"# 品味候选 · {date.today()}（近 {days} 天）", "",
         f"共 {len(items)} 条去重后的原话，其中 {sum(1 for i in items if i['score'] > 0)} 条带信号。"
         "这是原料，**不是结论**：整理进 taste/*.md 或项目 DECISIONS.md 前需要确认。", "",
         "## 推进型口令（零信息量，应变成默认授权）", "",
         "| 口令 | 次数 |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in nudges.most_common(15)]
    L.append("")
    for proj, _ in projects.most_common(20):
        L += [f"## {proj}", ""]
        for cat in CATEGORIES:
            rows = sorted(by.get((proj, cat), []), key=lambda i: (-i["score"], i["ts"]))[:per_bucket]
            if not rows:
                continue
            L.append(f"### {cat}")
            for r in rows:
                t = r["text"].replace("\n", " ⏎ ")
                t = t if len(t) <= 360 else t[:360] + "…"
                L.append(f"- `{r['ts'][:10]} {r['tool']}` {t}")
            L.append("")
    return "\n".join(L)


def run(days: int = 120, out_dir: str | None = None) -> Path:
    items, nudges = collect(days)
    out = Path(out_dir) if out_dir else TASTE_DIR / "candidates"
    stamp = date.today().isoformat()
    jl = out / f"{stamp}.jsonl"
    write_text(jl, "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
    md = out / f"{stamp}.md"
    write_text(md, render_md(items, nudges, days))
    write_text(out / "nudges.json", json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                                                "days": days, "counts": nudges.most_common()},
                                               ensure_ascii=False, indent=2))
    return md
