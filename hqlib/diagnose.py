"""hq diagnose & hq tune：确定性会话遥测诊断（X-Ray）与跨 Harness 高效工程护栏一键挂载。

支持三类会话源：
  1. DSH 会话（~/.dsh/sessions/**/session*.jsonl.zstd 或导出的 session.v4.jsonl）
  2. Codex CLI 会话（~/.codex/sessions/**/*.jsonl）
  3. Claude Code 会话（~/.claude/projects/*/*.jsonl）
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import ACCEPTANCE_FILE, DECISIONS_FILE, STATUS_FILE, find_project_root, load_config, read_json, write_text, write_json

HOME = Path.home()
DSH_SESSIONS_DIR = HOME / ".dsh" / "sessions"
CODEX_SESSIONS_DIR = HOME / ".codex" / "sessions"

SHELL_READ_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:sed\s+-n|cat\s+|head\s+-n?\s*\d+|tail\s+-n?\s*\d+|grep\s+-[rnE]+)\b"
)
TEST_CMD_RE = re.compile(r"\b(?:gradlew|pytest|cargo\s+test|pnpm\s+test|npm\s+test|go\s+test|unittest|just\s+(?:test|preflight))\b")
XML_LOG_PARSE_RE = re.compile(r"test-results|TEST-.*\.xml|ElementTree|<failure|\.hq/logs/")
DEVICE_REMOTE_RE = re.compile(r"\b(?:adb|ssh|scp|lldb|frida|idevicedebug|halo_adb)\b")
TIMID_PAUSE_RE = re.compile(r"(?:要不要|是否|需要我|请确认是否)(?:继续|执行|开始|改动)\s*[？?]?\s*$|should\s+I\s+(?:proceed|continue)\s*\?\s*$", re.I)
RAW_DECISION_HEADING_RE = re.compile(r"^##\s+D-\d{3}\b", re.MULTILINE)
MD_LINK_OR_URL_RE = re.compile(r"https?://|\[[^\]]+\]\([^)]+\)")


def read_jsonl_lines(path: Path) -> list[dict[str, Any]]:
    """读取普通 .jsonl 或 .jsonl.zstd 文件。"""
    raw = ""
    if path.suffix == ".zstd" or path.name.endswith(".jsonl.zstd"):
        zstd_bin = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
        if not Path(zstd_bin).is_file():
            return []
        try:
            proc = subprocess.run(
                [zstd_bin, "-dc", str(path)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            raw = proc.stdout or ""
        except Exception:
            return []
    else:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _slug_for_path(project_path: Path) -> str:
    # /Users/demo/code/demo-app -> --Users-demo-code-demo-app--
    cleaned = str(project_path.resolve()).strip("/").replace("/", "-")
    return f"--{cleaned}--"


def resolve_session_files(target: str | None, days: int = 14, limit: int = 5) -> tuple[Path | None, list[Path]]:
    """根据用户传入的目录、项目名、会话导出目录或 session-id 解析待分析的会话文件列表。"""
    cutoff = time.time() - days * 86400
    if target:
        cand = Path(target).expanduser()
        if cand.is_file() and (cand.name.endswith(".jsonl") or cand.name.endswith(".jsonl.zstd")):
            proj = _infer_project_from_session(cand)
            return proj, [cand.resolve()]
        if cand.is_dir():
            for name in ("session.v4.jsonl", "session.v4.jsonl.zstd", "session.jsonl", "session.jsonl.zstd"):
                f = cand / name
                if f.is_file():
                    proj = _infer_project_from_session(f)
                    return proj, [f.resolve()]
            proj_root = find_project_root(cand) or cand.resolve()
            return proj_root, _find_sessions_for_project(proj_root, cutoff, limit)

        for p in load_config().get("projects", []):
            root = Path(p["path"])
            if root.name.lower() == target.lower():
                return root, _find_sessions_for_project(root, cutoff, limit)

        matches: list[Path] = []
        for pat in (
            str(DSH_SESSIONS_DIR / "**" / f"*{target}*" / "session*.jsonl*"),
            str(CODEX_SESSIONS_DIR / "**" / f"*{target}*.jsonl"),
            str(HOME / "Downloads" / f"*{target}*" / "session*.jsonl*"),
        ):
            for hit in glob.glob(pat, recursive=True):
                hp = Path(hit)
                if hp.is_file():
                    matches.append(hp.resolve())
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            deduped: list[Path] = []
            seen_ids: set[str] = set()
            for m in matches:
                sid = re.sub(r"^dsh-session-", "", m.parent.name if m.name.startswith("session") else m.stem)
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    deduped.append(m)
            proj = _infer_project_from_session(deduped[0])
            return proj, deduped[:limit]

    cwd_root = find_project_root(os.getcwd()) or Path(os.getcwd()).resolve()
    return cwd_root, _find_sessions_for_project(cwd_root, cutoff, limit)


def _infer_project_from_session(session_file: Path) -> Path | None:
    for r in read_jsonl_lines(session_file)[:10]:
        cwd_val = r.get("cwd") or (r.get("payload") or {}).get("cwd")
        if r.get("type") in ("session", "session_meta") and cwd_val:
            cwd = Path(cwd_val)
            if cwd.is_dir():
                return find_project_root(cwd) or cwd
    return find_project_root(os.getcwd())


def _codex_session_matches_project(path: Path, resolved_root: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                if '"session_meta"' not in line[:300]:
                    continue
                payload = json.loads(line).get("payload") or {}
                cwd_str = payload.get("cwd") or ""
                if not cwd_str:
                    return False
                cwd = Path(cwd_str).resolve()
                return cwd == resolved_root or resolved_root in cwd.parents
    except (OSError, ValueError):
        return False
    return False


def _find_sessions_for_project(project_root: Path, cutoff: float, limit: int) -> list[Path]:
    resolved_root = project_root.resolve()
    slug = _slug_for_path(resolved_root)
    claude_slug = "-" + str(resolved_root).strip("/").replace("/", "-")
    all_files: list[Path] = []
    dsh_dir = DSH_SESSIONS_DIR / slug
    if dsh_dir.is_dir():
        for sdir in dsh_dir.iterdir():
            if not sdir.is_dir():
                continue
            versions = sorted(sdir.glob("session*.jsonl*"), key=lambda p: p.name, reverse=True)
            if versions:
                all_files.append(versions[0])
    claude_dir = HOME / ".claude" / "projects" / claude_slug
    if claude_dir.is_dir():
        all_files.extend(claude_dir.glob("*.jsonl"))

    if CODEX_SESSIONS_DIR.is_dir():
        codex_candidates = [
            Path(p) for p in glob.glob(str(CODEX_SESSIONS_DIR / "**" / "*.jsonl"), recursive=True)
        ]
        codex_candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
        matched_codex = 0
        for cp in codex_candidates[:80]:
            if cp.stat().st_mtime >= cutoff and _codex_session_matches_project(cp, resolved_root):
                all_files.append(cp)
                matched_codex += 1
                if matched_codex >= limit:
                    break

    recent = [f for f in all_files if f.exists() and f.stat().st_mtime >= cutoff]
    pool = recent if recent else [f for f in all_files if f.exists()]
    pool.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    non_trivial = [f for f in pool if f.stat().st_size >= 1500]
    return (non_trivial or pool)[:limit]


def _extract_command_str(args: Any) -> str:
    if isinstance(args, str):
        return args.strip()
    if not isinstance(args, dict):
        return ""
    raw = args.get("command") or args.get("cmd") or ""
    if isinstance(raw, list):
        if len(raw) >= 3 and raw[1] in ("-c", "-lc"):
            return str(raw[2]).strip()
        return " ".join(str(x) for x in raw).strip()
    return str(raw).strip()


def _record_shell_command(
    cmd: str,
    turn: int,
    step: int,
    shell_read_calls: list[dict[str, Any]],
    test_calls_ref: list[int],
    xml_parse_ref: list[int],
    device_calls_by_turn: Counter[int],
) -> None:
    if not cmd:
        return
    if SHELL_READ_RE.search(cmd):
        shell_read_calls.append({"turn": turn, "step": step, "cmd": cmd.replace("\n", " ")[:120]})
    if TEST_CMD_RE.search(cmd):
        test_calls_ref[0] += 1
    if XML_LOG_PARSE_RE.search(cmd):
        xml_parse_ref[0] += 1
    if DEVICE_REMOTE_RE.search(cmd):
        device_calls_by_turn[turn] += 1


def analyze_dsh_session(session_file: Path) -> dict[str, Any]:
    """对 DSH / Claude Code / Codex CLI 会话日志做确定性遥测分析。"""
    records = read_jsonl_lines(session_file)
    session_meta: dict[str, Any] = {}
    preset = "unknown"
    team_mode = False
    tool_counts: Counter[str] = Counter()
    turns: set[int] = set()
    timid_pause_count = 0
    steps_per_turn: Counter[int] = Counter()
    shell_read_calls: list[dict[str, Any]] = []
    test_calls = [0]
    xml_parse_calls = [0]
    device_calls_by_turn: Counter[int] = Counter()
    edited_files_by_turn: dict[int, set[str]] = defaultdict(set)
    failed_bash_calls: list[dict[str, Any]] = []
    calls_by_id: dict[str, dict[str, Any]] = {}

    for r in records:
        rtype = r.get("type")
        data = r.get("data") or {}
        if rtype == "session":
            session_meta = r
            preset = r.get("agentPreset", "unknown")
        elif rtype == "session_meta":
            pl = r.get("payload") or {}
            session_meta = {"id": pl.get("id") or session_file.stem, "cwd": pl.get("cwd") or ""}
            preset = "codex-cli"
        elif rtype == "agent-preset/selected":
            preset = data.get("agentPreset", preset)
        elif rtype == "team/mode":
            team_mode = bool(data.get("enabled"))
        elif rtype == "response_item":
            # Codex CLI JSONL format
            pl = r.get("payload") or {}
            ptype = pl.get("type")
            turn = max(turns) if turns else 1
            if ptype == "message" and pl.get("role") == "user":
                turn = (max(turns) + 1) if turns else 1
                turns.add(turn)
            elif ptype == "message" and pl.get("role") == "assistant":
                turns.add(turn)
                texts = [c.get("text", "") for c in (pl.get("content") or []) if isinstance(c, dict)]
                joined = "\n".join(t for t in texts if t).strip()
                if joined and TIMID_PAUSE_RE.search(joined):
                    timid_pause_count += 1
            elif ptype == "function_call":
                turns.add(turn)
                steps_per_turn[turn] += 1
                step = steps_per_turn[turn]
                raw_name = str(pl.get("name") or "").lower()
                call_id = str(pl.get("call_id") or "")
                norm = "bash" if raw_name in ("shell", "shell_command", "exec_command", "bash") else raw_name
                tool_counts[norm] += 1
                try:
                    args = json.loads(pl.get("arguments") or "{}")
                except ValueError:
                    args = {}
                cmd = _extract_command_str(args)
                if call_id:
                    calls_by_id[call_id] = {"turn": turn, "step": step, "name": norm, "cmd": cmd}
                if norm == "bash":
                    _record_shell_command(cmd, turn, step, shell_read_calls, test_calls, xml_parse_calls, device_calls_by_turn)
                elif norm in ("apply_patch", "edit", "write"):
                    fp = str(args.get("file_path") or args.get("path") or "")
                    if fp:
                        edited_files_by_turn[turn].add(fp)
            elif ptype == "function_call_output":
                call_id = str(pl.get("call_id") or "")
                call = calls_by_id.get(call_id)
                out_str = str(pl.get("output") or "")
                if call and call["name"] == "bash" and re.search(r"Process exited with code [1-9]", out_str):
                    failed_bash_calls.append({"turn": call["turn"], "step": call["step"], "cmd": call["cmd"][:100]})
        elif rtype == "assistant" and isinstance(r.get("message"), dict):
            # Claude Code .jsonl 兼容解析
            if preset == "unknown":
                preset = "claude-code"
            if not session_meta:
                session_meta = {"id": r.get("sessionId") or session_file.stem, "cwd": r.get("cwd") or ""}
            turn = max(turns) if turns else 1
            turns.add(turn)
            for block in r["message"].get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    txt = str(block.get("text") or "").strip()
                    if txt and TIMID_PAUSE_RE.search(txt):
                        timid_pause_count += 1
                    continue
                if block.get("type") != "tool_use":
                    continue
                steps_per_turn[turn] += 1
                step = steps_per_turn[turn]
                raw_name = str(block.get("name") or "").lower()
                name = "bash" if raw_name in ("bash", "shell", "shell_command", "exec_command") else raw_name
                tool_counts[name] += 1
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name in ("edit", "write"):
                    fp = str(args.get("file_path") or "")
                    if fp:
                        edited_files_by_turn[turn].add(fp)
                elif name == "bash":
                    cmd = _extract_command_str(args)
                    _record_shell_command(cmd, turn, step, shell_read_calls, test_calls, xml_parse_calls, device_calls_by_turn)
        elif rtype == "tool/call":
            turn = int(data.get("turn") or 0)
            step = int(data.get("step") or 0)
            name = str(data.get("name") or "")
            call_id = str(data.get("toolCallId") or "")
            turns.add(turn)
            steps_per_turn[turn] = max(steps_per_turn[turn], step)
            tool_counts[name] += 1
            try:
                args = json.loads(data.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls_by_id[call_id] = {"turn": turn, "step": step, "name": name, "args": args}

            if name in ("edit", "write"):
                fp = str(args.get("file_path") or "")
                if fp:
                    edited_files_by_turn[turn].add(fp)
            elif name == "bash":
                cmd = _extract_command_str(args)
                _record_shell_command(cmd, turn, step, shell_read_calls, test_calls, xml_parse_calls, device_calls_by_turn)
        elif rtype == "tool/result":
            call_id = str(data.get("toolCallId") or "")
            call = calls_by_id.get(call_id)
            if call and call["name"] == "bash":
                text = ""
                res_obj = data.get("result") or {}
                for c in res_obj.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text += c.get("text") or ""
                if "[exit code: " in text and "[exit code: 0]" not in text:
                    failed_bash_calls.append({
                        "turn": call["turn"],
                        "step": call["step"],
                        "cmd": _extract_command_str(call.get("args"))[:100],
                    })

    total_tools = sum(tool_counts.values())
    bash_count = tool_counts["bash"]
    read_count = tool_counts["read"]
    grep_count = tool_counts["grep"] + tool_counts["glob"]
    delegation_count = (
        tool_counts["subagent"]
        + tool_counts["subagent_fork"]
        + tool_counts["workflow"]
        + tool_counts["team_dispatch_batch"]
        + tool_counts["team_task_create"]
        + tool_counts["spawn_agent"]
    )

    solo_marathons = []
    for t, max_step in sorted(steps_per_turn.items()):
        edited = edited_files_by_turn.get(t, set())
        if max_step >= 35 and len(edited) >= 3 and delegation_count == 0:
            solo_marathons.append({"turn": t, "steps": max_step, "editedFiles": len(edited)})

    device_loops = [
        {"turn": t, "deviceCalls": cnt}
        for t, cnt in sorted(device_calls_by_turn.items())
        if cnt > 8
    ]

    return {
        "sessionId": session_meta.get("id") or session_file.parent.name,
        "file": str(session_file),
        "cwd": session_meta.get("cwd") or "",
        "preset": preset,
        "teamMode": team_mode,
        "turns": len(turns),
        "timidPauseCount": timid_pause_count,
        "totalSteps": sum(steps_per_turn.values()),
        "maxTurnSteps": max(steps_per_turn.values()) if steps_per_turn else 0,
        "totalToolCalls": total_tools,
        "bashCalls": bash_count,
        "bashRatio": round(bash_count / total_tools, 3) if total_tools else 0.0,
        "nativeReadCalls": read_count,
        "nativeSearchCalls": grep_count,
        "delegationCalls": delegation_count,
        "toolCounts": dict(tool_counts.most_common()),
        "bottlenecks": {
            "shellReadEscapeCount": len(shell_read_calls),
            "shellReadSamples": shell_read_calls[:5],
            "testRunCount": test_calls[0],
            "manualXmlOrLogParseCount": xml_parse_calls[0],
            "deviceLoopTurns": device_loops,
            "soloMarathonTurns": solo_marathons,
            "failedBashCount": len(failed_bash_calls),
            "failedBashSamples": failed_bash_calls[:5],
        },
    }


def check_doc_memory_hygiene(project_root: Path) -> tuple[bool, list[str]]:
    """检查 STATUS/DECISIONS/ACCEPTANCE 是否维持记忆融合形态而非编年 CRUD 堆积。"""
    issues: list[str] = []
    status_path = project_root / STATUS_FILE
    if status_path.is_file():
        st_text = status_path.read_text(encoding="utf-8", errors="ignore")
        human_part = st_text.split("<!-- hq:verify:start -->", 1)[0]
        human_lines = human_part.rstrip("\n").splitlines()
        if len(human_lines) > 85:
            issues.append(f"STATUS.md 人工区共 {len(human_lines)} 行（建议 <= 85 行，历史正文应归档至 .hq/history/）")
        summary_line = next((ln.strip()[1:].strip() for ln in human_lines if ln.strip().startswith(">")), "")
        if summary_line and (MD_LINK_OR_URL_RE.search(summary_line) or len(summary_line) > 65):
            issues.append(f"STATUS.md 首条引用摘要过长或含 URL（{len(summary_line)} 字符），会导致 `hq status` 截断")

    decisions_path = project_root / DECISIONS_FILE
    if decisions_path.is_file():
        dec_text = decisions_path.read_text(encoding="utf-8", errors="ignore")
        dec_lines = dec_text.splitlines()
        raw_d_headings = RAW_DECISION_HEADING_RE.findall(dec_text)
        if len(dec_lines) > 140 or len(raw_d_headings) > 8:
            issues.append(
                f"DECISIONS.md 膨胀为流水账（{len(dec_lines)} 行 / {len(raw_d_headings)} 条 `## D-xxx`），"
                "应按领域融合为可读记忆并归档原文"
            )

    acceptance_path = project_root / ACCEPTANCE_FILE
    if acceptance_path.is_file():
        acc_lines = acceptance_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(acc_lines) > 140:
            issues.append(f"ACCEPTANCE.md 共 {len(acc_lines)} 行（建议 <= 140 行并复用 just/npm 统一检查）")

    return len(issues) == 0, issues


def _has_duplicate_hq_gate(cfg: dict[str, Any]) -> bool:
    gate_cmds = 0
    for group in (cfg.get("hooks") or {}).get("Stop") or []:
        for h in group.get("hooks") or []:
            cmd = str(h.get("command") or "").strip()
            if cmd == "hq gate" or cmd.endswith(" hq gate"):
                gate_cmds += 1
    return gate_cmds > 1


def audit_project_harness(project_root: Path | None) -> dict[str, Any]:
    """检查目标项目的确定性代码、文档记忆预算、跨 Harness 同步与 Hook 完备度。"""
    if project_root is None or not project_root.is_dir():
        return {"projectRoot": None, "checks": {}, "hygieneNotes": [], "score": "0/7"}

    has_hq = (project_root / STATUS_FILE).is_file() and (project_root / ACCEPTANCE_FILE).is_file()
    doc_hygiene_ok, hygiene_notes = check_doc_memory_hygiene(project_root)

    agents_md = project_root / "AGENTS.md"
    claude_md = project_root / "CLAUDE.md"
    agents_aligned = False
    if agents_md.is_file():
        if not claude_md.exists() and not claude_md.is_symlink():
            agents_aligned = True
        elif claude_md.is_symlink():
            agents_aligned = claude_md.resolve() == agents_md.resolve()
        elif "@AGENTS.md" in claude_md.read_text(encoding="utf-8", errors="ignore"):
            agents_aligned = True

    justfiles = list(project_root.glob("[Jj]ustfile")) + list(project_root.glob("*/[Jj]ustfile"))
    just_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in justfiles)
    pkg_jsons = list(project_root.glob("package.json")) + list(project_root.glob("*/package.json"))
    pkg_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in pkg_jsons)
    has_det_recipes = (
        any(k in just_text for k in ("invariants", "preflight", "probe", "verify", "record-clip"))
        or '"lint"' in pkg_text
    )

    claude_cfg = read_json(project_root / ".claude" / "settings.json", {}) or {}
    codex_cfg = read_json(project_root / ".codex" / "hooks.json", {}) or {}
    claude_hooks = claude_cfg.get("hooks") or {}
    codex_hooks = codex_cfg.get("hooks") or {}
    has_lifecycle_hooks = (
        ("PreToolUse" in claude_hooks or "PreToolUse" in codex_hooks)
        and ("PostToolUse" in claude_hooks or "PostToolUse" in codex_hooks)
        and ("Stop" in claude_hooks or "Stop" in codex_hooks)
        and not _has_duplicate_hq_gate(claude_cfg)
        and not _has_duplicate_hq_gate(codex_cfg)
    )

    git_hooks = (
        list(project_root.glob(".git/hooks/pre-commit"))
        + list(project_root.glob("*/.git/hooks/pre-commit"))
        + list(project_root.glob(".githooks/pre-commit"))
    )
    has_git_hooks = any(p.is_file() and os.access(p, os.X_OK) for p in git_hooks)

    dsh_pkg = HOME / ".dsh" / "profiles" / "web" / "package.json"
    dsh_pkg_text = dsh_pkg.read_text(encoding="utf-8", errors="ignore") if dsh_pkg.is_file() else ""
    has_portable_harness = (
        "high-effective-develop" in dsh_pkg_text
        or (project_root / "scripts" / "harness_hook.py").is_file()
        or any(project_root.glob("*/scripts/harness_hook.py"))
        or any(project_root.glob("*/scripts/harness-hook.js"))
    )

    checks = {
        "hqProtocol": has_hq,
        "docMemoryHygiene": doc_hygiene_ok,
        "agentsInstructionAligned": agents_aligned,
        "deterministicJustRecipes": has_det_recipes,
        "lifecycleHooks4Stage": has_lifecycle_hooks,
        "gitPreCommitHook": has_git_hooks,
        "portableHarnessSupport": has_portable_harness,
    }
    passed = sum(1 for v in checks.values() if v)
    return {
        "projectRoot": str(project_root),
        "projectName": project_root.name,
        "checks": checks,
        "hygieneNotes": hygiene_notes,
        "score": f"{passed}/{len(checks)}",
    }


def generate_prescriptions(sessions: list[dict[str, Any]], harness: dict[str, Any]) -> list[str]:
    """根据遥测病灶与项目体检结果生成三层处方。"""
    rx: list[str] = []
    checks = harness.get("checks") or {}
    total_shell_reads = sum(s["bottlenecks"]["shellReadEscapeCount"] for s in sessions)
    total_xml_parses = sum(s["bottlenecks"]["manualXmlOrLogParseCount"] for s in sessions)
    total_timid = sum(s.get("timidPauseCount", 0) for s in sessions)
    has_device_loop = any(s["bottlenecks"]["deviceLoopTurns"] for s in sessions)
    has_solo_marathon = any(s["bottlenecks"]["soloMarathonTurns"] for s in sessions)

    if total_shell_reads > 10:
        rx.append(
            f"【1·工具纪律】检测到 {total_shell_reads} 次在 shell 中用 sed/cat/head/tail/grep 读源码："
            "应在 PreToolUse（匹配 `Bash|bash|shell|exec_command`）直接拦截，强制走原生文件读取/检索工具。"
        )
    if total_xml_parses > 2:
        rx.append(
            f"【1·确定性代码】检测到 {total_xml_parses} 次手写命令解析测试 XML/日志："
            "应封装一步跑测并自动提取失败断言文件行号的 `just test` 包装器。"
        )
    if total_timid > 0:
        rx.append(
            f"【1·自主契约】检测到 {total_timid} 次假性请示（`timid pause`）："
            "除缺凭据/高危资金/主观取舍外，按 `STATUS.md` `▶ 下一步` 自主推进闭环。"
        )
    if has_device_loop:
        rx.append(
            "【2·物理边界熔断】检测到单轮 >8 次 adb/ssh/lldb 外部设备盲点循环："
            "应封装去噪探测与 80/20 局部靶向录屏脚本（`just record-clip` 附盲模态文本遥测），并在 8 步时触发熔断转入 `❓`。"
        )
    if has_solo_marathon:
        rx.append(
            "【3·并发隔离】检测到单轮 >35 步串行改多个独立文件且 0 次委派："
            "遇到 >=2 个独立修复包时优先派发到隔离 worktree 或带互斥范围的并行子代理。"
        )
    if not checks.get("docMemoryHygiene"):
        for note in harness.get("hygieneNotes") or []:
            rx.append(f"【1·记忆融合】{note}")
    if not checks.get("agentsInstructionAligned"):
        rx.append(
            "【2·多 Harness 同源】建议维护精简 `AGENTS.md`，并让 `CLAUDE.md` 以 `@AGENTS.md` 或软链接指向同一入口，避免规则分叉。"
        )
    if not checks.get("lifecycleHooks4Stage"):
        proj = harness.get("projectRoot") or "."
        rx.append(
            f"【2·一键挂载护栏】当前项目尚未挂载跨 Harness 四段式 Hook（或 Stop 存在重复 `hq gate`），"
            f"请执行：`hq tune {proj} --apply`。"
        )
    if not checks.get("deterministicJustRecipes"):
        rx.append(
            "【1·确定性门禁】当前项目缺少 `invariants / preflight / probe / record-clip` 或 `lint` 配方，建议接入 `ACCEPTANCE.md` `quick` 档。"
        )
    if not rx:
        rx.append("✅ 当前会话与项目护栏状态健康，未发现显著注意力黑洞。")
    return rx


def render_diagnosis_report(
    project_root: Path | None,
    sessions: list[dict[str, Any]],
    harness: dict[str, Any],
    prescriptions: list[str],
) -> str:
    proj_title = project_root.name if project_root else "指定会话"
    lines = [
        f"# 🔍 Henry Better-Life · 效能诊断报告（{proj_title}）",
        "",
        f"- **项目路径**：`{harness.get('projectRoot') or '未绑定项目'}`",
        f"- **护栏完备度**：`{harness.get('score')}` "
        + " | ".join(f"{k}: {'✅' if v else '❌'}" for k, v in (harness.get("checks") or {}).items()),
        "",
    ]
    if not sessions:
        lines.append("> 近端未找到匹配的会话记录（可通过 `hq diagnose <session.jsonl路径>` 指定特定会话）。")
    else:
        lines.append("## 📊 会话遥测画像（Session Telemetry）")
        lines.append("")
        lines.append("| 会话 ID | 模式 | 轮数/总步数 | 最大单轮步数 | bash 占比 | timidPause | Shell读源码逃逸 | 跑测/扒XML | 设备盲点循环 | 并行委派 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---|---|---|")
        for s in sessions:
            b = s["bottlenecks"]
            dev_str = (
                ", ".join(f"T{d['turn']}:{d['deviceCalls']}次" for d in b["deviceLoopTurns"])
                if b["deviceLoopTurns"]
                else "0"
            )
            sid_short = str(s.get("sessionId") or "")[:16]
            preset_name = str(s.get("preset") or "unknown")
            lines.append(
                f"| `{sid_short}` | `{preset_name}` | {s['turns']}轮 / {s['totalSteps']}步 | "
                f"{s['maxTurnSteps']}步 | {int(s['bashRatio'] * 100)}% ({s['bashCalls']}/{s['totalToolCalls']}) | "
                f"{s.get('timidPauseCount', 0)}次 | {b['shellReadEscapeCount']}次 | "
                f"{b['testRunCount']}次 / {b['manualXmlOrLogParseCount']}次 | {dev_str} | {s['delegationCalls']}次 |"
            )
        lines.append("")

    lines.append("## 💊 三层确定性改造处方（Code · Hook · Multi-Harness）")
    lines.append("")
    for item in prescriptions:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


UNIVERSAL_HOOK_PY = '''#!/usr/bin/env python3
"""Universal Cross-Harness Lifecycle Hook installed by `hq tune`.

Works across Codex CLI, Claude Code, DSH, and Orca, including isolated git worktrees:
  - SessionStart: resolves git toplevel dynamically & injects STATUS.md snapshot
  - PreToolUse: blocks shell source-reading (`sed/cat/head/tail`) across Bash/bash/shell/exec_command
  - PostToolUse: 8-step external device/remote loop circuit breaker
  - Stop: fast STATUS.md / DECISIONS.md memory-hygiene check before `hq gate`
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FALLBACK_ROOT = Path(__file__).resolve().parents[1]
SHELL_TOOLS = frozenset({"bash", "shell", "shell_command", "exec_command", "run_terminal_cmd", "terminal"})

CODE_EXT_RE = re.compile(
    r"^\\s*(?:sed\\s+-n\\s+['\\\"][^'\\\"]*['\\\"]|cat|head(?:\\s+-\\d+|\\s+-n\\s*\\d+)?|tail(?:\\s+-\\d+|\\s+-n\\s*\\d+)?|nl|awk)\\s+"
    r"[^\\n|;&>]+\\.(?:kt|java|py|ts|tsx|js|rs|go|swift|c|cpp|h)\\s*$"
)
DEVICE_CMD_RE = re.compile(r"\\b(?:adb|ssh|lldb|frida)\\b")
RAW_D_RE = re.compile(r"^##\\s+D-\\d{3}\\b", re.MULTILINE)


def resolve_root(payload: dict) -> Path:
    cwd = str(payload.get("cwd") or os.getcwd())
    try:
        top = subprocess.check_output(
            ["git", "--no-optional-locks", "-C", cwd, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip()
        if top:
            return Path(top).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return FALLBACK_ROOT


def extract_cmd(tool_input: object) -> str:
    if isinstance(tool_input, str):
        return tool_input.strip()
    if not isinstance(tool_input, dict):
        return ""
    raw = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(raw, list):
        if len(raw) >= 3 and raw[1] in ("-c", "-lc"):
            return str(raw[2]).strip()
        return " ".join(str(x) for x in raw).strip()
    return str(raw).strip()


def state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")
    return Path(tempfile.gettempdir()) / f"hq-hed-state-{safe}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"session_id": session_id, "device_calls": 0}


def save_state(session_id: str, state: dict) -> None:
    try:
        state_path(session_id).write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    event = (sys.argv[1] if len(sys.argv) > 1 else None) or payload.get("hook_event_name") or ""
    session_id = str(payload.get("session_id") or "default")
    root = resolve_root(payload)
    tool_name = str(payload.get("tool_name") or "").strip().lower()

    if event == "SessionStart":
        status_file = root / "STATUS.md"
        summary = ""
        if status_file.is_file():
            summary = "\\n".join(status_file.read_text(encoding="utf-8", errors="ignore").splitlines()[:20])
        return emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"[HQ High-Effective Snapshot: {root.name}]\\n{summary}",
            }
        })

    if event == "PreToolUse" and tool_name in SHELL_TOOLS:
        cmd = extract_cmd(payload.get("tool_input"))
        if CODE_EXT_RE.match(cmd) and "<<" not in cmd and ">" not in cmd:
            return emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "⛔ [High-Effective Guard] 禁止在 shell 中用 sed/cat/head/tail 读取源码文件；"
                        "请直接使用原生文件读取或检索工具。"
                    ),
                }
            })

    if event == "PostToolUse" and tool_name in SHELL_TOOLS:
        cmd = extract_cmd(payload.get("tool_input"))
        if DEVICE_CMD_RE.search(cmd):
            state = load_state(session_id)
            state["device_calls"] = int(state.get("device_calls", 0)) + 1
            save_state(session_id, state)
            if state["device_calls"] >= 8 and state["device_calls"] % 4 == 0:
                return emit({
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": (
                            f"⚠️ [8-Step Circuit Breaker] 当前会话已连续执行 {state['device_calls']} 次外部设备/远程命令。"
                            "若属物理环境/真人交互/主观审美限制，请立即停止盲试并归入 STATUS.md 的 `❓` 栏。"
                        ),
                    }
                })

    if event == "Stop":
        decisions = root / "DECISIONS.md"
        if decisions.is_file():
            dec_text = decisions.read_text(encoding="utf-8", errors="ignore")
            if len(RAW_D_RE.findall(dec_text)) > 12:
                return emit({
                    "decision": "block",
                    "reason": "⛔ [HQ Memory Hygiene] DECISIONS.md 堆积超过 12 条 `## D-xxx`，请按领域融合记忆并将原文归档至 .hq/history/。",
                })

    return emit({})


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _dedupe_stop_gates(stop_groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """确保 Stop 下最多保留 1 次 `hq gate`，避免与自定义 Hook 重复跑两次 verify。"""
    cleaned: list[dict[str, Any]] = []
    seen_gate = False
    changed = False
    for g in stop_groups:
        items = list(g.get("hooks") or [])
        kept = []
        for h in items:
            cmd = str(h.get("command") or "").strip()
            if cmd == "hq gate" or cmd.endswith(" hq gate"):
                if seen_gate:
                    changed = True
                    continue
                seen_gate = True
            kept.append(h)
        if kept:
            cleaned.append({**g, "hooks": kept})
    return cleaned, changed


def tune_project(project_root: Path, apply: bool = False) -> list[str]:
    """为指定项目检查或一键挂载跨 Harness 四段式高效开发 Hook 护栏（与单次 hq gate 共存）。"""
    root = find_project_root(project_root) or project_root.resolve()
    actions: list[str] = []
    # 如果项目已有自定义 tools/hooks/high_effective_hook.py，优先复用它
    custom_hook = root / "tools" / "hooks" / "high_effective_hook.py"
    hook_script = custom_hook if custom_hook.is_file() else (root / ".hq" / "high_effective_hook.py")

    if not hook_script.is_file():
        if apply:
            write_text(hook_script, UNIVERSAL_HOOK_PY)
            os.chmod(hook_script, 0o755)
            actions.append(f"已生成跨 Harness 高效开发 Hook 脚本: {hook_script.relative_to(root)}")
        else:
            actions.append(f"[待生成] 跨 Harness 高效开发 Hook 脚本: {hook_script.relative_to(root)}")
    else:
        actions.append(f"复用已有高效开发 Hook 脚本: {hook_script.relative_to(root)}")

    for cfg_rel in (Path(".claude/settings.json"), Path(".codex/hooks.json")):
        cfg_path = root / cfg_rel
        cfg = read_json(cfg_path, {}) if cfg_path.is_file() else {}
        if not isinstance(cfg, dict):
            cfg = {}
        hooks = cfg.setdefault("hooks", {})
        changed = False
        for event_name, matcher, timeout in (
            ("SessionStart", None, 10),
            ("PreToolUse", "Bash|bash|shell|shell_command|exec_command", 10),
            ("PostToolUse", "Bash|bash|shell|shell_command|exec_command", 15),
            ("Stop", None, 15),
        ):
            cmd_str = f"python3 -B {hook_script} {event_name}"
            groups = hooks.setdefault(event_name, [])
            already = any(
                "high_effective_hook.py" in (h.get("command") or "") or "harness_hook.py" in (h.get("command") or "")
                for g in groups
                for h in (g.get("hooks") or [])
            )
            if not already:
                entry: dict[str, Any] = {
                    "hooks": [{
                        "type": "command",
                        "command": cmd_str,
                        "timeout": timeout,
                        "statusMessage": f"High-Effective {event_name} guard",
                    }],
                }
                if matcher:
                    entry["matcher"] = matcher
                groups.insert(0 if event_name == "Stop" else len(groups), entry)
                changed = True

        stop_groups, deduped = _dedupe_stop_gates(hooks.setdefault("Stop", []))
        if deduped:
            hooks["Stop"] = stop_groups
            changed = True

        has_gate = any(
            "hq" in (h.get("command") or "") and (h.get("command") or "").endswith(" gate")
            for g in stop_groups
            for h in (g.get("hooks") or [])
        )
        if not has_gate:
            stop_groups.append({
                "hooks": [{
                    "type": "command",
                    "command": "hq gate",
                    "timeout": 330,
                    "statusMessage": "hq 验收闸门",
                }]
            })
            changed = True

        if changed:
            if apply:
                write_json(cfg_path, cfg)
                actions.append(f"已挂载四段式 Hook (SessionStart/PreToolUse/PostToolUse/Stop) → {cfg_rel}")
            else:
                actions.append(f"[待挂载] 四段式 Hook → {cfg_rel}（加 `--apply` 立即写入）")
        else:
            actions.append(f"四段式 Hook 已就绪: {cfg_rel}")

    return actions
