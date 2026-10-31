"""Install only HQ's auto-default hooks; preserve all other client settings."""
from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .common import write_json

AGENTS = ("codex", "qoder", "zcode")
PATHS = {
    "codex": ".codex/hooks.json",
    "qoder": ".qoder/settings.json",
    "zcode": ".zcode/cli/config.json",
}
ENTRY = Path(__file__).resolve().parents[1] / "bin" / "hq.py"
TIMEOUT = 330


def config_path(agent: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / PATHS[agent]


def handler(agent: str, event: str, python: str | None = None) -> dict:
    argv = [python or sys.executable, str(ENTRY), "auto-default", "--agent", agent]
    timeout = TIMEOUT if event == "Stop" else 10
    if agent == "zcode":
        return {"type": "process", "command": argv[0], "args": argv[1:],
                "enabled": True, "timeoutMs": timeout * 1000}
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return {"type": "command", "command": command, "timeout": timeout}


def _owned(item: dict) -> bool:
    # Match our explicit entry point or the old bare `hq auto-default` command.
    command = item.get("command", "")
    args = item.get("args", [])
    return command == "hq auto-default" or (
        "auto-default" in (command + " " + " ".join(args))
        and ("hq.py" in command or any(Path(a).name == "hq.py" for a in args))
    )


def desired_config(cfg: dict, agent: str) -> dict:
    result = copy.deepcopy(cfg)
    hooks = result.setdefault("hooks", {})
    if agent == "zcode":
        # Do not silently re-enable disabled third-party hooks.
        if hooks.get("enabled") is False:
            raise ValueError("ZCode hooks are explicitly disabled; enable them in ZCode settings first")
        hooks["enabled"] = True
        events = hooks.setdefault("events", {})
    else:
        events = hooks
    for event in ("Stop", "UserPromptSubmit"):
        groups = events.setdefault(event, [])
        retained = []
        python = None
        for group in groups:
            items = group.get("hooks", [])
            for item in items:
                if not _owned(item):
                    continue
                try:
                    argv = ([item["command"], *item.get("args", [])] if agent == "zcode"
                            else shlex.split(item["command"], posix=os.name != "nt"))
                    argv = [arg.strip('"') for arg in argv]
                    if (argv[1:] == [str(ENTRY), "auto-default", "--agent", agent]
                            and Path(argv[0]).is_absolute() and Path(argv[0]).is_file()):
                        # `hq` and `python bin/hq.py` may use different Python
                        # versions; keep a working installed interpreter stable.
                        python = argv[0]
                except (ValueError, KeyError):
                    pass
            other = [h for h in items if not _owned(h)]
            if other or not items:
                retained.append(dict(group, hooks=other))
        retained.append({"hooks": [handler(agent, event, python)]})
        events[event] = retained
    return result


def install(agent: str, *, check: bool = False, home: Path | None = None) -> dict:
    path = config_path(agent, home)
    # Strict parsing: malformed configuration is never replaced.
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: expected a JSON object")
    desired = desired_config(cfg, agent)
    current = cfg == desired
    if not check and not current:
        if path.exists():
            # One original backup per client. It stays beside the private config.
            backup = path.with_name(path.name + ".hq-backup")
            if not backup.exists():
                with backup.open("x", encoding="utf-8") as out:
                    out.write(path.read_text(encoding="utf-8"))
                backup.chmod(0o600)
        write_json(path, desired)
        path.chmod(0o600)
    return {"agent": agent, "path": str(path), "current": current if check else True,
            "changed": not current and not check}


def run(args) -> int:
    failed = False
    for agent in args.agents:
        try:
            result = install(agent, check=args.check)
            print(json.dumps(result, ensure_ascii=False))
            failed |= not result["current"]
        except (ValueError, OSError, TypeError, AttributeError) as error:
            print(f"{agent}: {error}", file=sys.stderr)
            failed = True
    return int(failed)
