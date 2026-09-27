"""hq brief：早晚简报。只列需要 Henry 注意的东西，其余一行带过。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import BRIEFS_DIR, HQ_HOME, STATUS_FILE, load_config, write_text
from .fingerprint import code_fingerprint
from .status import parse_status
from .verify import load_verify, summarize

TOKEI_SCRIPT = Path.home() / ".tokei" / "usage.30s.py"
_NOISE = ("/scratch-workspaces/", "/.claude/worktrees/", "/worktrees/", "/private/tmp", "/tmp/")


TOKEI_CACHE = HQ_HOME / ".hq" / "tokei.cache.json"
TOKEI_TTL_SECONDS = 600  # 定时简报天然刷新；人工 hq brief 大多命中缓存


def tokei_projects(script: Path = TOKEI_SCRIPT, *, force: bool = False) -> list:
    """tokei 全量采集较慢（数秒级）；人工 brief 命中 10 分钟内的缓存即可。"""
    if not force and TOKEI_CACHE.exists():
        try:
            if time.time() - TOKEI_CACHE.stat().st_mtime < TOKEI_TTL_SECONDS:
                return json.loads(TOKEI_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # 缓存坏了就当没有
    if not script.exists():
        return []
    try:
        out = subprocess.run(["python3", str(script), "--projects"], capture_output=True,
                             timeout=120, check=True)
        data = json.loads(out.stdout or b"[]")
        try:
            TOKEI_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TOKEI_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _gate_events(root: Path, since: datetime) -> dict:
    counts = {}
    try:
        for line in (root / ".hq" / "gate.log").read_text(encoding="utf-8").splitlines():
            ts, event, *_ = line.split("\t") + [""]
            if ts >= since.isoformat(timespec="seconds"):
                counts[event] = counts.get(event, 0) + 1
    except OSError:
        pass
    return counts


def collect(cfg: dict = None, tokei: list = None) -> list:
    cfg = cfg or load_config()
    tokei = tokei if tokei is not None else tokei_projects()
    by_path = {t.get("path"): t for t in tokei}
    since = datetime.now() - timedelta(hours=24)
    rows = []
    for p in cfg["projects"]:
        root = Path(p["path"])
        st = parse_status(root / STATUS_FILE)
        fp = code_fingerprint(root) if root.exists() else ""
        ver = summarize(load_verify(root), fp)
        t = by_path.get(str(root), {})
        updated_day = (st or {}).get("updated", "")[:10]
        rows.append({
            "root": root, "status": st, "verify": ver, "tokei": t,
            "gate": _gate_events(root, since),
            "stale": bool(st and t.get("last_active") and updated_day and t["last_active"] > updated_day),
        })
    return rows


def unregistered(tokei: list, registered: set, days: int = 7, limit: int = 8) -> list:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    home = str(Path.home())
    out = [t for t in tokei
           if t.get("last_active", "") >= cutoff and t.get("path") not in registered
           and t.get("path") != home and not any(n in t.get("path", "") for n in _NOISE)
           and (t.get("cost") or 0) >= 1]
    out.sort(key=lambda t: t.get("cost") or 0, reverse=True)
    return out[:limit]


def _link(path: Path) -> str:
    return f"[{path.name}](file://{path})"


def render(rows: list, tokei: list, cfg: dict, slot: str) -> str:
    today = date.today().isoformat()
    label = {"am": "早", "pm": "晚"}.get(slot, "")
    active = [r for r in rows if r["status"] and r["status"]["state"] == "active"]
    n_you = sum(len(r["status"]["needs_you"]) for r in rows if r["status"])
    n_block = sum(len(r["status"]["blocked"]) for r in rows if r["status"])
    bad = [r for r in rows if r["verify"]["failed"] or r["gate"].get("gave-up")]
    wip = cfg.get("wip_limit", 3)
    L = [f"# HQ 简报 · {today} {label}", "",
         f"> 待你判断 **{n_you}** · 阻塞 **{n_block}** · 验收异常 **{len(bad)}** · "
         f"主线 {len(active)}/{wip}" + (" ⚠ 超过上限" if len(active) > wip else ""), ""]

    L += ["## 🧑‍⚖️ 待你判断", ""]
    any_you = False
    for r in rows:
        st = r["status"]
        if not st or not (st["needs_you"] or st["answered"]):
            continue
        any_you = True
        L.append(f"### {st['name']}（{st['theme']} · {st['value']}） {_link(Path(st['path']))}")
        if st["summary"]:
            L.append(f"_{st['summary']}_")
        L += [f"- [ ] {i['text'].split(' — ')[0]}" for i in st["needs_you"]]
        if st["answered"]:
            L.append(f"- （你已答复 {len(st['answered'])} 项，等 agent 下一轮消化）")
        L.append("")
    if not any_you:
        L += ["无。", ""]

    blocked = [(r["status"]["name"], i["text"].split("：")[0].split(" — ")[0])
               for r in rows if r["status"] for i in r["status"]["blocked"]]
    if blocked:
        L += ["## ⛔ 阻塞", ""] + [f"- **{n}**：{t}" for n, t in blocked] + [""]

    alerts = []
    for r in rows:
        name = r["status"]["name"] if r["status"] else r["root"].name
        v = r["verify"]
        if v["failed"]:
            alerts.append(f"- **{name}**：检查未通过 {', '.join(f'`{x}`' for x in v['failed'])}")
        if v["stale"] and not v["failed"]:
            alerts.append(f"- **{name}**：代码已变，{len(v['stale'])}/{v['total']} 项检查未复验（`hq verify --tier full`）")
        if v["not_run"]:
            alerts.append(f"- **{name}**：{', '.join(f'`{x}`' for x in v['not_run'])} 未运行（条件不满足，见日志）")
        if v["total"] == 0:
            alerts.append(f"- **{name}**：还没有任何机器验收记录")
        if r["gate"].get("gave-up"):
            alerts.append(f"- **{name}**：近 24h 闸门驳回后放弃 {r['gate']['gave-up']} 次（agent 没能自证通过）")
        if r["stale"]:
            alerts.append(f"- **{name}**：{r['tokei'].get('last_active')} 有活动，但 STATUS 停在 {r['status']['updated']}（空转？）")
    if alerts:
        L += ["## 🔴 需要留意", ""] + alerts + [""]

    L += ["## 🗂 组合视图", "",
          "| 项目 | 主题 | 价值 | 状态 | ❓ | ⛔ | 机器验收 | 最近活跃 | 累计花费 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        st, v, t = r["status"], r["verify"], r["tokei"]
        if not st:
            L.append(f"| {r['root'].name} | – | – | 未接入 | | | | {t.get('last_active', '')} | |")
            continue
        vv = f"{v['passed']}/{v['total']}" + (" ⚠" if v["stale"] or v["failed"] else "")
        cost = f"${t['cost']:.0f}" if t.get("cost") else ""
        L.append(f"| {st['name']} | {st['theme']} | {st['value']} | {st['state']} | {len(st['needs_you'])} | "
                 f"{len(st['blocked'])} | {vv} | {t.get('last_active', '')} | {cost} |")
    L.append("")

    registered = {str(r["root"]) for r in rows}
    new = unregistered(tokei, registered)
    if new:
        L += ["## 🆕 近 7 天活跃但未接入", "",
              "接入：`hq init <路径> --theme … --value …`；不打算管就忽略。", ""]
        L += [f"- {t['name']} — ${t.get('cost', 0):.0f} · {t.get('sessions')} 会话 · {t.get('last_active')} · `{t['path']}`"
              for t in new]
        L.append("")
    return "\n".join(L)


def notify(title: str, text: str) -> None:
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    subprocess.run(["osascript", "-e", script, title, text], capture_output=True)


def sync_from_brief(path: Path | None = None) -> list:
    """把简报里 Typora 勾选的 `- [x] 标题——意见` 写回各项目 STATUS。

    简报是视图不是源头；Henry 在 Typora 里勾选/批注后跑一次 sync，
    才真正落到 STATUS（与 hq ok / hq note 同一条 answer() 通路）。
    匹配规则：项目名来自 ### 标题；条目按标题对回 待答 列表——
    对不上（简报过期 / 标题被改）就跳过并在返回值里如实报告。
    返回 [(id, 意见 or None)]，只含本次真正应用了的。
    """
    from . import answer

    path = path or BRIEFS_DIR / "latest.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    pending: dict[str, dict[str, str]] = {}
    for rid, state, title in answer.todo():
        if state != "待答":
            continue
        proj = rid.rsplit("#", 1)[0]
        pending.setdefault(proj, {})[title] = rid
    applied = []
    skipped = []
    current = None
    for line in lines:
        h = re.match(r"^###\s+(\S+?)（", line)
        if h:
            current = h.group(1)
            continue
        m = re.match(r"^- \[x\]\s+(.*)$", line)
        if not m or current is None:
            continue
        body = m.group(1).strip().replace("**", "")
        note = None
        for sep in ("——", "—", "--"):
            if sep in body:
                head, tail = body.split(sep, 1)
                if tail.strip():
                    note, body = tail.strip(), head.strip()
                break
        title = body.split(" — ")[0].strip()
        rid = pending.get(current, {}).get(title)
        if rid is None:
            skipped.append(f"{current}: {title[:30]}")
            continue
        answer.answer(rid, note)
        applied.append((rid, note))
    return applied, skipped


def run_brief(slot: str = None, write: bool = True, do_notify: bool = False,
                fresh: bool = False) -> tuple:
    slot = slot or ("am" if datetime.now().hour < 14 else "pm")
    # 覆盖前先把旧简报里 Typora 勾选的 [x]/批注写回 STATUS（以 Henry 的修改为准）。
    # 能弄丢这些标记的唯一路径就是本函数的重新生成——在这里 sync，标记结构性不会丢。
    synced = []
    if write:
        try:
            synced, _skipped = sync_from_brief()
        except OSError:
            synced = []  # 旧简报读不到就当没有
    cfg = load_config()
    tokei = tokei_projects(force=fresh)
    rows = collect(cfg, tokei)
    text = render(rows, tokei, cfg, slot)
    path = None
    if write:
        path = BRIEFS_DIR / f"{date.today().isoformat()}-{slot}.md"
        write_text(path, text)
        write_text(BRIEFS_DIR / "latest.md", text)
    if synced:
        print(f"（简报勾选已自动写回 {len(synced)} 条）", file=sys.stderr)
    if do_notify:
        n_you = sum(len(r["status"]["needs_you"]) for r in rows if r["status"])
        n_bad = sum(1 for r in rows if r["verify"]["failed"])
        notify("HQ 简报", f"待你判断 {n_you} 项 · 验收异常 {n_bad} 个项目")
    return text, path
