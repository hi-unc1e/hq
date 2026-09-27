"""hq gate：Claude Code / Codex 的 Stop hook。

stdin 收到 hook JSON（含 cwd / session_id / stop_hook_active），stdout 必须输出合法 JSON：
放行输出 {}，驳回输出 {"decision": "block", "reason": "..."}（两家格式一致）。
任何内部异常都放行（fail-open），并记到 .hq/gate.log。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .common import (ACCEPTANCE_FILE, STATUS_FILE, find_project_root, read_json,
                     state_dir, write_json)
from .fingerprint import code_fingerprint
from .status import parse_checks, parse_status, select_checks
from .verify import load_verify, run_verify

MAX_BLOCKS = 2  # 同一轮最多驳回次数；Codex 自身不设上限，必须由 hook 兜底。
TAIL_IN_REASON = 25


def _gate_log(root: Path, event: str, detail: str = "") -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{event}\t{detail}\n"
    with open(state_dir(root) / "gate.log", "a", encoding="utf-8") as fh:
        fh.write(line)


def _status_hash(root: Path) -> str | None:
    st = parse_status(root / STATUS_FILE)
    return st["human_hash"] if st else None


def evaluate(payload: dict) -> dict:
    root = find_project_root(payload.get("cwd") or os.getcwd())
    if root is None:
        return {}
    state_file = state_dir(root) / "gate.json"
    state = read_json(state_file, {}) or {}
    sessions = state.setdefault("sessions", {})
    sid = str(payload.get("session_id") or "unknown")
    sess = sessions.setdefault(sid, {"blocks": 0})
    sess["seen"] = datetime.now().isoformat(timespec="seconds")
    if not payload.get("stop_hook_active"):
        sess["blocks"] = 0
    # 只保留最近 50 个会话的计数
    if len(sessions) > 50:
        for old in sorted(sessions, key=lambda k: sessions[k].get("seen", ""))[:-50]:
            sessions.pop(old, None)

    def save():
        write_json(state_file, state)

    def block(kind: str, reason: str) -> dict:
        sess["blocks"] = sess.get("blocks", 0) + 1
        _gate_log(root, f"block:{kind}", sid)
        save()
        return {"decision": "block", "reason": reason}

    if sess.get("blocks", 0) >= MAX_BLOCKS:
        _gate_log(root, "gave-up", sid)
        sess["blocks"] = 0
        save()
        return {}

    fp = code_fingerprint(root)
    base = state.get("baseline")
    if not base:
        state["baseline"] = {"fp": fp, "status_hash": _status_hash(root)}
        _gate_log(root, "baseline", fp)
        save()
        return {}
    if fp == base.get("fp"):
        base["status_hash"] = _status_hash(root)
        save()
        return {}

    # 代码自上次通过后有变化 → quick 检查
    quick = select_checks(parse_checks(root / ACCEPTANCE_FILE), "quick")
    data = load_verify(root)
    needs_run = any(
        data["checks"].get(c["id"], {}).get("fingerprint") != fp
        or not data["checks"].get(c["id"], {}).get("ok")
        for c in quick
    )
    if quick and needs_run:
        res = run_verify(root, "quick", quiet=True)
        fp = res["fingerprint"]
        if any(not r["ok"] and not r["not_run"] for r in res["results"].values()):
            parts = []
            for cid, r in res["results"].items():
                if r["ok"] or r["not_run"]:
                    continue
                why = "超时" if r["timed_out"] else f"exit {r['exit']}"
                tail = "\n".join(r["tail"].splitlines()[-TAIL_IN_REASON:])
                parts.append(f"### {cid}（{why}）: `{r['cmd']}`\n完整日志: {r['log']}\n```\n{tail}\n```")
            return block("verify", (
                "[hq 验收闸门] quick 检查未通过，本轮不能结束。\n\n" + "\n\n".join(parts) +
                "\n\n请修复后再结束。若失败与你本轮改动无关（环境问题或并行会话的改动），"
                f"在 {root / STATUS_FILE} 的「⛔ 阻塞」里写明原因和证据，然后结束；"
                "若与用户本轮的明确要求冲突，同样写进 ⛔ 让 Henry 裁决，不要擅自撤销用户要的改动。"))

    if _status_hash(root) == base.get("status_hash"):
        return block("status", (
            f"[hq 验收闸门] 代码有改动，但 {root / STATUS_FILE} 没有更新。按 henry-hq 协议收工：\n"
            "1. front matter 的 updated；\n"
            "2. `> 一句话现状`（结果，不是过程）；\n"
            "3. `## ❓ 待 Henry 判断`：只放机器验证不了的事，写清怎么验、你的建议；\n"
            "4. `## ⛔ 阻塞` 与 `## ▶ 下一步`。\n"
            "不要手写 ✅ 机器区（由 hq verify 生成）。"
            "如果这些改动来自并行会话而非你本轮，在 STATUS 里一句话说明即可。"))

    state["baseline"] = {"fp": fp, "status_hash": _status_hash(root)}
    sess["blocks"] = 0
    _gate_log(root, "pass", fp)
    save()
    return {}


def main(stdin=None) -> int:
    stdin = stdin or sys.stdin
    if getattr(stdin, "isatty", lambda: False)():
        # 人工手敲：gate 等 stdin JSON 会一直挂着，且这不是给你用的命令。
        print("hq gate 是机器专用命令（Stop hook 通过 stdin 供给 JSON），这里直接退出。")
        print("人工用：hq todo 看盘 · hq status 总览 · hq verify 跑验收")
        root = find_project_root(os.getcwd())
        if root is not None:
            try:
                last = (state_dir(root) / "gate.log").read_text(
                    encoding="utf-8").splitlines()[-1]
            except OSError:
                last = ""
            print(f"最近闸门事件：{last or '（无）'}")
        return 0
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        out = evaluate(payload)
    except Exception:  # fail-open：闸门出错绝不能卡死 agent
        out = {}
        try:
            root = find_project_root(os.getcwd())
            if root:
                _gate_log(root, "error", traceback.format_exc().replace("\n", " | ")[-800:])
        except Exception:
            pass
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0
