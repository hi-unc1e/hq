"""Native Windows smoke and integration tests; run by windows.yml."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hqlib.verify import shell_command  # noqa: E402


@unittest.skipUnless(os.name == "nt", "requires Windows")
class WindowsIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "hq.toml"
        self.env = dict(os.environ, HQ_CONFIG=str(self.config), HQ_PYTHON=sys.executable)
        self.launcher = ROOT / "bin" / "hq.cmd"

    def hq(self, *args, cwd=None, input_text=None):
        return subprocess.run(["cmd", "/d", "/c", str(self.launcher), *args],
                              cwd=cwd or self.root, env=self.env, input=input_text,
                              capture_output=True, text=True, timeout=60)

    def test_launcher_and_gate(self):
        help_out = self.hq("--help")
        self.assertEqual(help_out.returncode, 0, help_out.stderr)
        self.assertIn("henry-hq", help_out.stdout)
        gate = self.hq("gate", input_text=json.dumps({"cwd": str(self.root)}))
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertEqual(json.loads(gate.stdout), {})

    def test_init_config_and_verify(self):
        project = self.root / "project"
        project.mkdir()
        init = self.hq("init", str(project), "--name", "demo")
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertTrue((project / ".claude" / "settings.json").exists())
        config = self.config.read_text(encoding="utf-8")
        self.assertIn(project.as_posix(), config)
        self.assertNotIn("\\", config)

        (project / "ACCEPTANCE.md").write_text(
            '```hq-checks\nquick unit python -c "print(42)"\n```\n', encoding="utf-8")
        verify = self.hq("verify", "--tier", "quick", cwd=project)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("1/1", verify.stdout)
        self.assertIn("42", (project / ".hq" / "logs" / "unit.log").read_text())

    def test_native_shell_selection(self):
        argv, kwargs = shell_command("echo hello")
        self.assertEqual(argv[-4:], ["/d", "/s", "/c", "echo hello"])
        self.assertIn("creationflags", kwargs)


if __name__ == "__main__":
    unittest.main()
