"""Codex Stop hook support for applying a safe recommended option after a timeout.

The hook only arms when the assistant emits the explicit machine-readable marker
and the matching visible A/B/C option is uniquely labelled 最推荐. It observes
the append-only session transcript while Stop is running so a new user message
cancels the default. On timeout, Stop returns a continuation prompt.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

WAIT_SECONDS = 300
POLL_SECONDS = 0.5
STATE_DIR = Path.home() / ".codex" / "hq-auto-default"
MARKER_RE = re.compile(r"<!--\s*hq-auto-default:\s*(\{.*?\})\s*-->", re.DOTALL)
OPTION_RE = re.compile(r"^\s*(?:[-*]\s*)?([A-C])[.)、:：]\s*(.+?)\s*$", re.MULTILINE)
RISK_RE = re.compile(
    r"删除|覆写|覆盖|发布|推送|部署|生产|付款|付费|购买|发送|外发|对外|外部|邮件|短信|密码|凭据|密钥|令牌|权限|安全设置|授权|不可逆|破坏性|"
    r"\b(delete|overwrite|publish|push|deploy|production|purchase|buy|pay|payment|send|external|email|sms|password|credential|secret|token|permission|security setting|authorization|irreversible|destructive)\b",
    re.IGNORECASE,
)


def parse_default(message: str | None) -> dict | None:
    """Return the recommended option only for an explicit, safe question marker."""
    if not message:
        return None
    markers = list(MARKER_RE.finditer(message))
    if len(markers) != 1 or message[markers[0].end():].strip():
        return None
    try:
        meta = json.loads(markers[0].group(1))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("eligible") is not True or meta.get("choice") not in {"A", "B", "C"}:
        return None

    visible = MARKER_RE.sub("", message)
    if "?" not in visible and "？" not in visible:
        return None
    options = {m.group(1): m.group(2).strip() for m in OPTION_RE.finditer(visible)}
    if len(options) < 2 or meta["choice"] not in options:
        return None
    recommended = [m.group(1) for m in OPTION_RE.finditer(visible) if "最推荐" in m.group(2)]
    if recommended != [meta["choice"]]:
        return None
    choice = re.sub(r"\s*[（(]?最推荐[）)]?\s*$", "", options[meta["choice"]]).strip()
    if not choice or RISK_RE.search(choice):
        return None
    return {"label": meta["choice"], "choice": choice}


def _has_new_user_message(path: Path, offset: int) -> bool | None:
    """Read only bytes appended since the question was emitted."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                payload = row.get("payload", {})
                item = payload.get("item", {}) if isinstance(payload, dict) else {}
                if item.get("type") == "UserMessage":
                    return True
                if row.get("type") == "UserMessage":
                    return True
    except OSError:
        # Unknown transcript state must never be treated as user silence.
        return None
    return False


def _log(event: str, sid: str, detail: str = "") -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "events.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\t{event}\t{sid}\t{detail}\n")
    except OSError:
        pass


def cancel_pending(payload: dict) -> None:
    """UserPromptSubmit callback: mark an active timer as answered/cancelled."""
    sid = str(payload.get("session_id") or "unknown")
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", sid) + "-"
    try:
        for lock in STATE_DIR.glob(prefix + "*.lock"):
            try:
                lock.with_suffix(".cancel").write_text("user prompt submitted\n", encoding="utf-8")
            except OSError:
                pass
    except OSError:
        pass


def evaluate(payload: dict, *, wait_seconds: float | None = None,
             poll_seconds: float | None = None) -> dict:
    """Arm the five-minute timer and either cancel or continue with the default.

    ``wait_seconds`` and ``poll_seconds`` are injectable solely for tests. The
    production duration is fixed at five minutes and cannot be changed by model
    output or environment variables.
    """
    if payload.get("hook_event_name") == "UserPromptSubmit":
        cancel_pending(payload)
        return {}
    if payload.get("hook_event_name") not in (None, "Stop"):
        return {}
    if payload.get("stop_hook_active"):
        return {}
    selected = parse_default(payload.get("last_assistant_message"))
    if selected is None:
        return {}
    transcript = payload.get("transcript_path")
    if not transcript:
        return {}
    transcript_path = Path(transcript).expanduser()
    try:
        offset = transcript_path.stat().st_size
    except OSError:
        return {}

    sid = str(payload.get("session_id") or "unknown")
    turn_id = str(payload.get("turn_id") or "turn")
    # Duplicate global + project hooks can arrive together. Only one owns the
    # timer and continuation for this thread/turn.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}-{re.sub(r'[^A-Za-z0-9_.-]', '_', turn_id)}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {}
    except OSError:
        return {}

    duration = WAIT_SECONDS if wait_seconds is None else max(0.0, wait_seconds)
    interval = POLL_SECONDS if poll_seconds is None else max(0.01, poll_seconds)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        cancel_file = lock.with_suffix(".cancel")
        deadline = time.monotonic() + duration
        while True:
            # A reply wins even at the timeout boundary.
            new_user_message = _has_new_user_message(transcript_path, offset)
            if cancel_file.exists() or new_user_message is True:
                _log("cancelled:user-reply", sid, turn_id)
                return {}
            if new_user_message is None:
                _log("cancelled:transcript-unavailable", sid, turn_id)
                return {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        _log("expired:apply-default", sid, selected["label"])
        reason = (
            "[HQ_AUTO_DEFAULT_TIMEOUT] 用户在问题送达后五分钟内没有回复；"
            f"自动采用最推荐选项 {selected['label']}：{selected['choice']}。"
            "这是已标记的可逆低风险选择。请继续当前任务并记录该默认选择；"
            "如果执行时发现它需要删除或覆盖数据、发布/外发、付费、生产部署、"
            "修改安全/权限、使用凭据或扩大授权范围，立即停止并请求明确授权。"
        )
        return {"decision": "block", "reason": reason}
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock.unlink()
        except OSError:
            pass
        try:
            lock.with_suffix(".cancel").unlink()
        except OSError:
            pass


def main(stdin=None) -> int:
    stdin = stdin or sys.stdin
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = evaluate(payload)
    except Exception:
        # Hook failures must not trap or fail an otherwise normal Codex turn.
        result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0
