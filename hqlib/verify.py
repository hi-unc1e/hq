"""hq verify：跑 ACCEPTANCE.md 里的机器检查，结果写 .hq/verify.json 并刷新 STATUS 机器区。"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .common import (ACCEPTANCE_FILE, STATUS_FILE, log, read_json, state_dir,
                     write_json, write_text)
from .fingerprint import code_fingerprint
from .status import parse_checks, replace_verify_block, select_checks

TAIL_LINES = 40
NOT_RUN_EXIT = 3  # 约定：检查脚本以 3 退出表示“条件不满足，未运行”（如额度用尽），既不算过也不算挂


def verify_path(root: Path) -> Path:
    return state_dir(root) / "verify.json"


def load_verify(root: Path) -> dict:
    data = read_json(Path(root) / ".hq" / "verify.json", {}) or {}
    data.setdefault("checks", {})
    return data


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def run_check(root: Path, check: dict) -> dict:
    logs = state_dir(root) / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{check['id']}.log"
    started = time.monotonic()
    timed_out = False
    with open(log_path, "wb") as fh:
        fh.write(f"$ {check['cmd']}\n".encode())
        fh.flush()
        proc = subprocess.Popen(["/bin/zsh", "-lc", check["cmd"]], cwd=root, stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=check["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            code = proc.wait()
    secs = round(time.monotonic() - started, 1)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    return {
        "tier": check["tier"],
        "cmd": check["cmd"],
        "ok": code == 0 and not timed_out,
        "not_run": code == NOT_RUN_EXIT and not timed_out,
        "exit": code,
        "timed_out": timed_out,
        "secs": secs,
        "at": datetime.now().isoformat(timespec="seconds"),
        "log": str(log_path),
        "tail": _tail(output),
    }


def run_verify(root, tier: str = "quick", quiet: bool = False, update_status: bool = True) -> dict:
    root = Path(root).resolve()
    checks = select_checks(parse_checks(root / ACCEPTANCE_FILE), tier)
    data = load_verify(root)
    results = {}
    for check in checks:
        if not quiet:
            log(f"▶ [{check['tier']}] {check['id']}: {check['cmd']}")
        res = run_check(root, check)
        results[check["id"]] = res
        data["checks"][check["id"]] = res
        if not quiet:
            mark = _mark(res)
            log(f"  {mark}  {res['secs']}s  日志: {res['log']}")
            if not res["ok"]:
                log("  " + res["tail"].replace("\n", "\n  "))
    # 指纹在检查跑完之后取：检查自己生成的文件（缓存、产物）不应让结果显得“过期”。
    fp = code_fingerprint(root)
    for res in results.values():
        res["fingerprint"] = fp
    # ACCEPTANCE 里已删除的检查不再保留，避免陈旧结果冒充证据。
    known = {c["id"] for c in parse_checks(root / ACCEPTANCE_FILE)}
    data["checks"] = {k: v for k, v in data["checks"].items() if k in known}
    ok = all(r["ok"] for r in results.values())
    data["last_run"] = {"at": datetime.now().isoformat(timespec="seconds"), "tier": tier,
                        "ok": ok, "ids": list(results), "fingerprint": fp}
    write_json(verify_path(root), data)
    if update_status:
        refresh_status_block(root, data, fp)
    return {"ok": ok, "results": results, "fingerprint": fp}


def _mark(r: dict) -> str:
    if r.get("ok"):
        return "✅"
    if r.get("timed_out"):
        return "⏱ 超时"
    if r.get("not_run"):
        return "⏸ 未运行"
    return f"❌ exit {r.get('exit')}"


def summarize(data: dict, current_fp: str | None = None) -> dict:
    checks = data.get("checks", {})
    passed = [k for k, v in checks.items() if v.get("ok")]
    stale = [k for k, v in checks.items() if current_fp and v.get("fingerprint") != current_fp]
    last = max((v.get("at", "") for v in checks.values()), default="")
    return {"total": len(checks), "passed": len(passed),
            "failed": [k for k, v in checks.items() if not v.get("ok") and not v.get("not_run")],
            "not_run": [k for k, v in checks.items() if v.get("not_run")],
            "stale": stale, "last_at": last}


def render_block(data: dict, current_fp: str) -> str:
    checks = data.get("checks", {})
    if not checks:
        return "_尚未运行 `hq verify`_"
    lines = [f"_由 `hq verify` 自动生成，勿手改 · 代码指纹 `{current_fp[:8]}`_", "",
             "| 检查 | 档 | 结果 | 耗时 | 时间 | 对应当前代码 |", "|---|---|---|---|---|---|"]
    for cid, r in checks.items():
        result = _mark(r)
        at = (r.get("at") or "")[5:16].replace("T", " ")
        fresh = "是" if r.get("fingerprint") == current_fp else "⚠ 代码已变"
        lines.append(f"| `{cid}` | {r.get('tier')} | {result} | {r.get('secs')}s | {at} | {fresh} |")
    return "\n".join(lines)


def refresh_status_block(root: Path, data: dict = None, fp: str = None) -> None:
    status = Path(root) / STATUS_FILE
    if not status.is_file():
        return
    data = data if data is not None else load_verify(root)
    fp = fp or code_fingerprint(root)
    text = status.read_text(encoding="utf-8")
    new = replace_verify_block(text, render_block(data, fp))
    if new != text:
        write_text(status, new)
