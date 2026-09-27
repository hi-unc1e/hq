import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import gate, install, status  # noqa: E402
from hqlib.fingerprint import code_fingerprint  # noqa: E402
from hqlib.verify import load_verify, run_verify  # noqa: E402

STATUS = """---
hq: 1
project: demo
theme: 游戏
value: 好玩
state: active
updated: 2026-09-26 09:00
---
# demo · 状态

> 联机房间号已上线，等真机确认。

## ❓ 待 Henry 判断
- [ ] 真机试听鸭叫 — 怎么验：… — 建议：有意见就写 `→ Henry: …`
- [x] 已看过的截图
- [ ] 水雾浓度 → Henry: 再淡一点
- 无

## ⛔ 阻塞
- 无

## ▶ 下一步（agent 自主推进，无需回复）
- 补 e2e

## ✅ 机器验收

<!-- hq:verify:start -->
- [ ] 这里的列表不算
<!-- hq:verify:end -->
"""

ACCEPT = """# demo
```hq-checks
quick unit  true
full  e2e   timeout=5 sh -c 'exit 4'
```
"""


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class StatusParsing(unittest.TestCase):
    def test_sections_and_counts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "STATUS.md"
            p.write_text(STATUS, encoding="utf-8")
            st = status.parse_status(p)
        self.assertEqual(st["name"], "demo")
        self.assertEqual(st["theme"], "游戏")
        self.assertEqual(st["summary"], "联机房间号已上线，等真机确认。")
        self.assertEqual([i["text"][:6] for i in st["needs_you"]], ["真机试听鸭叫"])
        self.assertEqual(len(st["answered"]), 2)  # 勾选 + 批注
        self.assertEqual(st["blocked"], [])       # “无”不算
        self.assertEqual(len(st["next"]), 1)

    def test_human_hash_ignores_machine_block(self):
        a = status.human_hash(STATUS)
        b = status.human_hash(status.replace_verify_block(STATUS, "| x | ✅ |"))
        c = status.human_hash(STATUS.replace("补 e2e", "补单测"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_replace_block_appends_when_missing(self):
        out = status.replace_verify_block("# t\n", "hello")
        self.assertIn(status.VERIFY_START, out)
        self.assertIn("hello", out)

    def test_parse_checks(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ACCEPTANCE.md"
            p.write_text(ACCEPT, encoding="utf-8")
            checks = status.parse_checks(p)
            self.assertEqual([(c["tier"], c["id"], c["timeout"]) for c in checks],
                             [("quick", "unit", 120), ("full", "e2e", 5)])
            self.assertEqual(checks[1]["cmd"], "sh -c 'exit 4'")
            self.assertEqual(len(status.select_checks(checks, "quick")), 1)
            p.write_text("```hq-checks\nslow x y\n```\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                status.parse_checks(p)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        (self.root / "STATUS.md").write_text(STATUS, encoding="utf-8")
        (self.root / "ACCEPTANCE.md").write_text(ACCEPT, encoding="utf-8")
        (self.root / ".gitignore").write_text(".hq/\n", encoding="utf-8")
        (self.root / "app.py").write_text("print(1)\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def stop(self, active=False, sid="s1"):
        payload = {"cwd": str(self.root / "sub" if (self.root / "sub").exists() else self.root),
                   "session_id": sid, "stop_hook_active": active}
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            gate.main(io.StringIO(json.dumps(payload)))
        finally:
            sys.stdout = old
        return json.loads(out.getvalue())

    def touch_code(self, text="print(2)\n"):
        time.sleep(0.01)
        (self.root / "app.py").write_text(text, encoding="utf-8")

    def edit_status(self, new_next):
        p = self.root / "STATUS.md"
        p.write_text(p.read_text(encoding="utf-8").replace("补 e2e", new_next), encoding="utf-8")


class Fingerprint(ProjectCase):
    def test_protocol_files_do_not_change_fingerprint(self):
        fp = code_fingerprint(self.root)
        self.edit_status("别的")
        (self.root / ".hq").mkdir(exist_ok=True)
        (self.root / ".hq" / "x").write_text("1")
        self.assertEqual(fp, code_fingerprint(self.root))
        self.touch_code()
        self.assertNotEqual(fp, code_fingerprint(self.root))

    def test_non_git_tree(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            (r / "a.c").write_text("x")
            (r / "target").mkdir()
            (r / "target" / "big.o").write_text("x")
            fp = code_fingerprint(r)
            (r / "target" / "big.o").write_text("yy")
            (r / "STATUS.md").write_text("s")
            self.assertEqual(fp, code_fingerprint(r))
            time.sleep(0.01)
            (r / "a.c").write_text("yz")
            self.assertNotEqual(fp, code_fingerprint(r))


class Verify(ProjectCase):
    def test_full_run_records_results_and_status_block(self):
        res = run_verify(self.root, "full", quiet=True)
        self.assertFalse(res["ok"])
        data = load_verify(self.root)
        self.assertTrue(data["checks"]["unit"]["ok"])
        self.assertEqual(data["checks"]["e2e"]["exit"], 4)
        text = (self.root / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn("`unit`", text)
        self.assertIn("❌ exit 4", text)
        self.assertNotIn("这里的列表不算", text)

    def test_exit_3_is_not_run_not_failure(self):
        (self.root / "ACCEPTANCE.md").write_text("```hq-checks\nfull e2e sh -c 'echo quota; exit 3'\n```\n")
        run_verify(self.root, "full", quiet=True)
        from hqlib.verify import summarize
        s = summarize(load_verify(self.root))
        self.assertEqual((s["failed"], s["not_run"]), ([], ["e2e"]))
        self.assertIn("⏸ 未运行", (self.root / "STATUS.md").read_text(encoding="utf-8"))

    def test_timeout_is_enforced(self):
        (self.root / "ACCEPTANCE.md").write_text("```hq-checks\nquick slow timeout=1 sleep 5\n```\n")
        t = time.monotonic()
        res = run_verify(self.root, "quick", quiet=True)
        self.assertLess(time.monotonic() - t, 4)
        self.assertTrue(res["results"]["slow"]["timed_out"])


class Gate(ProjectCase):
    def test_first_stop_sets_baseline(self):
        self.assertEqual(self.stop(), {})
        self.assertTrue((self.root / ".hq" / "gate.json").exists())

    def test_no_change_passes(self):
        self.stop()
        self.assertEqual(self.stop(), {})

    def test_code_change_without_status_update_is_blocked(self):
        self.stop()
        self.touch_code()
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("STATUS.md 没有更新", out["reason"])
        self.edit_status("补 e2e 与截图")
        self.assertEqual(self.stop(active=True), {})
        # 通过后成为新基线
        self.assertEqual(self.stop(), {})

    def test_failing_quick_check_blocks_with_output(self):
        (self.root / "ACCEPTANCE.md").write_text(
            "```hq-checks\nquick unit sh -c 'echo BOOM-OUTPUT; exit 4'\n```\n", encoding="utf-8")
        self.stop()
        self.touch_code()
        self.edit_status("x")
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("BOOM-OUTPUT", out["reason"])
        self.assertIn("exit 4", out["reason"])

    def test_gives_up_after_two_blocks_per_turn(self):
        self.stop()
        self.touch_code()
        self.assertEqual(self.stop()["decision"], "block")
        self.assertEqual(self.stop(active=True)["decision"], "block")
        self.assertEqual(self.stop(active=True), {})
        log = (self.root / ".hq" / "gate.log").read_text(encoding="utf-8")
        self.assertIn("gave-up", log)
        # 新一轮（stop_hook_active=False）重新计数
        self.assertEqual(self.stop()["decision"], "block")

    def test_artifacts_created_by_checks_do_not_cause_false_block(self):
        # 真实 Codex 端到端中发现：检查命令生成 __pycache__ / 产物，导致下一次 Stop 误判“代码又变了”。
        (self.root / "ACCEPTANCE.md").write_text(
            "```hq-checks\nquick unit sh -c 'mkdir -p __pycache__ && date > __pycache__/x.pyc && date > out.tmp'\n```\n",
            encoding="utf-8")
        self.stop()
        self.touch_code()
        self.edit_status("改了 app")
        self.assertEqual(self.stop(), {})
        self.assertEqual(self.stop(), {})  # 不能因为 out.tmp 被误判为“STATUS 未更新”

    def test_outside_project_passes(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            old = sys.stdout
            sys.stdout = out
            try:
                gate.main(io.StringIO(json.dumps({"cwd": d})))
            finally:
                sys.stdout = old
            self.assertEqual(json.loads(out.getvalue()), {})

    def test_garbage_input_fails_open(self):
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            gate.main(io.StringIO("not json"))
        finally:
            sys.stdout = old
        self.assertEqual(out.getvalue(), "{}")


class Hooks(unittest.TestCase):
    def test_install_hook_is_idempotent_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".claude" / "settings.json"
            p.parent.mkdir()
            p.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                     "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
            self.assertTrue(install.install_hook(p))
            self.assertFalse(install.install_hook(p))
            cfg = json.loads(p.read_text())
            self.assertEqual(cfg["permissions"]["allow"], ["Bash(ls)"])
            self.assertEqual(len(cfg["hooks"]["Stop"]), 2)
            self.assertEqual(cfg["hooks"]["Stop"][1]["hooks"][0]["command"], "hq gate")


class Init(unittest.TestCase):
    def test_init_creates_files_hooks_and_registers_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "proj"
            root.mkdir()
            (root / "STATUS.md").write_text("keep me")
            cfg = Path(d) / "hq.toml"
            old = install.CONFIG_PATH
            install.CONFIG_PATH = cfg
            try:
                install.init_project(root, name="proj", theme="游戏", value="好玩")
                again = install.init_project(root, name="proj")
            finally:
                install.CONFIG_PATH = old
            self.assertEqual((root / "STATUS.md").read_text(), "keep me")
            acc = (root / "ACCEPTANCE.md").read_text(encoding="utf-8")
            self.assertIn("proj · 验收标准", acc)
            self.assertIn(".hq/", (root / ".gitignore").read_text())
            for hp in (root / ".claude" / "settings.json", root / ".codex" / "hooks.json"):
                self.assertTrue(install._has_gate(json.loads(hp.read_text())))
            self.assertEqual(cfg.read_text().count("[[projects]]"), 1)
            self.assertFalse(any("登记" in x or "安装" in x for x in again))


class Mine(unittest.TestCase):
    def test_redacts_secrets_before_writing_candidates(self):
        from hqlib.mine import NUDGE_RE, classify, redact
        out = redact("环境：sshpass -p 'Gc1[n3.m6(U' ssh root@1.2.3.4；token: abcDEF123456；sk-proj-ABCDEFGHIJKLMNOPQRSTUV")
        for secret in ("Gc1[n3", "abcDEF123456", "ABCDEFGHIJKLMNOP"):
            self.assertNotIn(secret, out)
        self.assertEqual(redact("普通的话 /Users/demo/projects/demo-game"), "普通的话 /Users/demo/projects/demo-game")
        self.assertTrue(NUDGE_RE.match("go on please"))
        self.assertFalse(NUDGE_RE.match("继续，但把水雾调淡一点，之前说过甬道要加速"))
        self.assertIn("审美", classify("现在的 blackhat.css 实在太丑"))


class FusedSyntax(unittest.TestCase):
    def test_expand_fused_maps_to_current_project(self):
        import os

        from hqlib import answer, common
        from hqlib.cli import _expand_fused
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa = d / "aaa"; aaa.mkdir()
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{aaa}"\n', encoding="utf-8")
            old, oldcwd = common.CONFIG_PATH, Path.cwd()
            common.CONFIG_PATH = cfg
            os.chdir(aaa)
            try:
                self.assertEqual(
                    _expand_fused(["ok#2"]), ["ok", "aaa#2"])
                self.assertEqual(
                    _expand_fused(["note#3", "太吵"]), ["note", "aaa#3", "太吵"])
                # 非融合形式 / 其他子命令不动
                self.assertEqual(_expand_fused(["todo", "-a"]), ["todo", "-a"])
                self.assertEqual(_expand_fused(["ok", "aaa#1"]), ["ok", "aaa#1"])
                # note 正文里的 ok#1 不会被误伤（融合只在子命令位）
                self.assertEqual(
                    _expand_fused(["note", "aaa#1", "see ok#1"]),
                    ["note", "aaa#1", "see ok#1"])
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old

    def test_expand_fused_outside_project_fails_helpfully(self):
        import tempfile as _tf

        from hqlib.cli import _expand_fused
        with _tf.TemporaryDirectory() as d:
            oldcwd = Path.cwd()
            os.chdir(d)  # 临时目录不在任何登记项目里（用真实注册表）
            try:
                with self.assertRaises(SystemExit) as cm:
                    _expand_fused(["ok#1"])
                self.assertIn("写全编号", str(cm.exception))
            finally:
                os.chdir(oldcwd)


class SpacedShortId(unittest.TestCase):
    def test_expand_id_maps_hash_n_to_current_project(self):
        import os

        from hqlib import common
        from hqlib.cli import _expand_id
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa = d / "aaa"; aaa.mkdir()
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{aaa}"\n', encoding="utf-8")
            old, oldcwd = common.CONFIG_PATH, Path.cwd()
            common.CONFIG_PATH = cfg
            os.chdir(aaa)
            try:
                self.assertEqual(_expand_id("#2"), "aaa#2")
                self.assertEqual(_expand_id("#10"), "aaa#10")
                # 裸数字同样映射（最顺手的形式，脚本里也无需引号）
                self.assertEqual(_expand_id("2"), "aaa#2")
                self.assertEqual(_expand_id("10"), "aaa#10")
                # 完整编号与无关参数原样返回（note 正文安全由 cmd 层结构保证）
                self.assertEqual(_expand_id("aaa#1"), "aaa#1")
                self.assertEqual(_expand_id("demo#3"), "demo#3")
                self.assertEqual(_expand_id("普通文本"), "普通文本")
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old

    def test_expand_id_outside_project_fails_helpfully(self):
        import tempfile as _tf

        from hqlib.cli import _expand_id
        with _tf.TemporaryDirectory() as d:
            oldcwd = Path.cwd()
            os.chdir(d)
            try:
                with self.assertRaises(SystemExit) as cm:
                    _expand_id("#1")
                self.assertIn("写全编号", str(cm.exception))
            finally:
                os.chdir(oldcwd)


class Answer(unittest.TestCase):
    def test_todo_scopes_to_cwd_project(self):
        """默认视角 = 当前目录所在项目；不在项目里 = 全部；--all 走 CLI 层。"""
        import os

        from hqlib import answer, common
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa, bbb = d / "aaa", d / "bbb"
            aaa.mkdir(); bbb.mkdir()
            # fixture 的 front matter 写死 project: demo —— 按目录改写，避免同名碰撞
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            (bbb / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: bbb"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(
                f'[[projects]]\npath = "{aaa}"\n\n[[projects]]\npath = "{bbb}"\n',
                encoding="utf-8")
            old = common.CONFIG_PATH
            oldcwd = Path.cwd()
            common.CONFIG_PATH = cfg
            os.chdir(aaa)
            try:
                self.assertEqual(answer.current_project(), "aaa")
                rows = answer.todo(scope=answer.current_project())
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(r[0].startswith("aaa#") for r in rows))
                # 子目录也算项目内
                sub = aaa / "src"; sub.mkdir()
                os.chdir(sub)
                self.assertEqual(answer.current_project(), "aaa")
                # 项目外 → None；todo() 不带 scope 仍是全量
                os.chdir(d)
                self.assertIsNone(answer.current_project())
                self.assertEqual(len(answer.todo()), 6)
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old

    def test_todo_ok_note_roundtrip_keeps_numbering(self):
        from hqlib import answer, common
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "demo"
            root.mkdir()
            (root / "STATUS.md").write_text(STATUS, encoding="utf-8")
            cfg = Path(d) / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{root}"\n', encoding="utf-8")
            old = common.CONFIG_PATH
            common.CONFIG_PATH = cfg
            try:
                rows = answer.todo()
                self.assertEqual([(r[0], r[1]) for r in rows],
                                 [("demo#1", "待答"), ("demo#2", "已通过"), ("demo#3", "已批注")])
                answer.answer("demo#1", "太吵了，再轻一点")
                answer.answer("DEMO#3")          # 大小写不敏感；已批注的也可以再勾选
                rows = answer.todo()
                self.assertEqual([r[1] for r in rows], ["已批注", "已通过", "已通过"])
                st = status.parse_status(root / "STATUS.md")
                self.assertEqual(st["needs_you"], [])
                self.assertEqual(len(st["answered"]), 3)
                self.assertNotEqual(st["updated"], "2026-09-26 09:00")
                text = (root / "STATUS.md").read_text(encoding="utf-8")
                self.assertIn("真机试听鸭叫 — 怎么验：… — 建议：有意见就写 `→ Henry: …` → Henry: 太吵了，再轻一点", text)
                self.assertIn(status.VERIFY_START, text)
                with self.assertRaises(SystemExit):
                    answer.answer("demo#9")
                with self.assertRaises(SystemExit):
                    answer.answer("nope#1")
            finally:
                common.CONFIG_PATH = old


class BriefSync(unittest.TestCase):
    def test_sync_applies_typora_checkmarks_and_notes(self):
        import os

        from hqlib import answer, brief, common
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa = d / "aaa"; aaa.mkdir()
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{aaa}"\n', encoding="utf-8")
            old, oldcwd, olddir = common.CONFIG_PATH, Path.cwd(), brief.BRIEFS_DIR
            common.CONFIG_PATH = cfg
            brief.BRIEFS_DIR = d
            os.chdir(aaa)
            try:
                # 简报视图：Typora 里勾了第 1 条并写意见；第 3 条纯通过；第 2 条留空
                (d / "latest.md").write_text(
                    "### aaa（游戏 · 好玩）\n"
                    "- [x] **真机试听鸭叫**——太轻一点\n"
                    "- [ ] 已看过的截图\n"          # 未勾选 → 忽略
                    "- [x] **不存在的条目**\n",     # 对不上 → 如实跳过
                    encoding="utf-8")
                applied, skipped = brief.sync_from_brief()
                ids = {rid for rid, _ in applied}
                self.assertEqual(len(skipped), 1)
                self.assertEqual(ids, {"aaa#1"})
                rows = {r[0]: r[1] for r in answer.todo()}
                self.assertEqual(rows["aaa#1"], "已批注")
                self.assertIn("太轻一点", (aaa / "STATUS.md").read_text(encoding="utf-8"))
                # 幂等：已答复的不再出现在待答映射里，第二次 sync 无事发生
                applied2, skipped2 = brief.sync_from_brief()
                self.assertEqual([r for r in applied2 if r[0] in ids], [])
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old
                brief.BRIEFS_DIR = olddir


class ItemDetail(unittest.TestCase):
    def test_detail_renders_labels_and_note(self):
        import os

        from hqlib import answer, common
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa = d / "aaa"; aaa.mkdir()
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{aaa}"\n', encoding="utf-8")
            old, oldcwd = common.CONFIG_PATH, Path.cwd()
            common.CONFIG_PATH = cfg
            os.chdir(aaa)
            try:
                card1 = answer.render_detail(answer.item_detail("aaa#1"))
                self.assertIn("怎么验：", card1)          # 标签分行
                card3 = answer.render_detail(answer.item_detail("aaa#3"))
                self.assertIn("你的批注: 再淡一点", card3)  # 批注独立成行
                card = card1 + card3
                self.assertIn("hq note 3", card)
                self.assertIn(str(aaa / "STATUS.md"), card)
                # 未知编号与 todo 一致的报错
                with self.assertRaises(SystemExit):
                    answer.item_detail("aaa#9")
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old


class BriefAutoSync(unittest.TestCase):
    def test_regeneration_syncs_marks_before_overwrite(self):
        """Henry 在 Typora 勾了旧简报 → 下次生成（手动或定时）先写回 STATUS 再覆盖，
        新简报里该条变为"你已答复"，标记结构性不会丢。"""
        import os

        from hqlib import brief, common
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            aaa = d / "aaa"; aaa.mkdir()
            (aaa / "STATUS.md").write_text(
                STATUS.replace("project: demo", "project: aaa"), encoding="utf-8")
            cfg = d / "hq.toml"
            cfg.write_text(f'[[projects]]\npath = "{aaa}"\n', encoding="utf-8")
            # 旧简报：勾了第 1 条并带意见
            (d / "latest.md").write_text(
                "### aaa（游戏 · 好玩）\n- [x] **真机试听鸭叫**——太轻一点\n",
                encoding="utf-8")
            old, oldcwd, olddir = common.CONFIG_PATH, Path.cwd(), brief.BRIEFS_DIR
            common.CONFIG_PATH = cfg
            brief.BRIEFS_DIR = d
            os.chdir(d)  # 项目外 → collect 也安静
            try:
                real_tokei = brief.tokei_projects
                brief.tokei_projects = lambda *a, **k: []  # 不跑采集
                try:
                    text, path = brief.run_brief(write=True)
                finally:
                    brief.tokei_projects = real_tokei     # 别污染后续测试
                body = (aaa / "STATUS.md").read_text(encoding="utf-8")
                self.assertIn("太轻一点", body)              # 写回了
                states = {r[0]: r[1] for r in __import__("hqlib.answer", fromlist=["todo"]).todo()}
                self.assertEqual(states["aaa#1"], "已批注")
                self.assertNotIn("- [x] **真机试听鸭叫**", text)  # 新简报不再列为待答
                # 再生成一次：已答复的不在待答映射，幂等无事发生
                brief.tokei_projects = lambda *a, **k: []
                try:
                    text2, _ = brief.run_brief(write=True)
                finally:
                    brief.tokei_projects = real_tokei
                self.assertIn("你已答复", text2)
            finally:
                os.chdir(oldcwd)
                common.CONFIG_PATH = old
                brief.BRIEFS_DIR = olddir


class ManualVsMachine(unittest.TestCase):
    """人工/机器命令分离：gate 手敲（TTY）快速友好退出；brief 命中 tokei 缓存。"""

    def test_gate_tty_exits_fast_with_hint(self):
        import io as _io

        from hqlib import gate
        out = _io.StringIO()
        old_out, old_in = sys.stdout, sys.stdin
        class _Tty(_io.StringIO):
            def isatty(self):
                return True
        sys.stdout = out
        sys.stdin = _Tty("")
        try:
            rc = gate.main()
        finally:
            sys.stdout, sys.stdin = old_out, old_in
        self.assertEqual(rc, 0)
        self.assertIn("机器专用", out.getvalue())
        self.assertIn("hq todo", out.getvalue())

    def test_tokei_cache_hit_skips_collection(self):
        import tempfile as _tf

        from hqlib import brief
        with _tf.TemporaryDirectory() as d:
            cache = Path(d) / "tokei.cache.json"
            cache.write_text(json.dumps([{"path": "/x", "name": "x"}]), encoding="utf-8")
            old = brief.TOKEI_CACHE
            brief.TOKEI_CACHE = cache
            try:
                # script 路径不存在：命中缓存则根本不会去采集
                rows = brief.tokei_projects(script=Path(d) / "nope.py")
            finally:
                brief.TOKEI_CACHE = old
            self.assertEqual(rows, [{"path": "/x", "name": "x"}])


class Launcher(unittest.TestCase):
    """入口 = 仓库内 bin/hq（软链接到 PATH 亦可）。"""

    ENTRY = Path(__file__).resolve().parents[1] / "bin" / "hq"

    def _entry_or_skip(self):
        if not self.ENTRY.is_file():
            self.skipTest(f"入口不在本机: {self.ENTRY}")
        return self.ENTRY

    def test_symlinked_launcher_finds_repo(self):
        hq = self._entry_or_skip()
        with tempfile.TemporaryDirectory() as d:
            # 两级符号链接也必须能找到仓库（PATH 最小化模拟 hook 环境）
            deep = Path(d) / "hq_cli"
            deep.symlink_to(hq)
            link = Path(d) / "hq"
            link.symlink_to(deep)
            out = subprocess.run([str(link), "--help"], capture_output=True,
                                 env=dict(os.environ, PATH="/usr/bin:/bin"))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(b"henry-hq", out.stdout)

    def test_gate_via_entry_outputs_json(self):
        hq = self._entry_or_skip()
        env = dict(os.environ, PATH="/usr/bin:/bin")  # 模拟 hook 的最小 PATH
        out = subprocess.run([str(hq), "gate"], input=b'{"cwd": "/"}', capture_output=True, env=env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), {})


if __name__ == "__main__":
    unittest.main()
