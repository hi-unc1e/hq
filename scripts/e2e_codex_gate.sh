#!/bin/sh
# 真实 Codex 端到端：agent 改了代码却没更新 STATUS → 闸门驳回 → agent 按协议更新 STATUS → 放行。
# 会消耗少量 Codex 额度（低推理强度，一个小任务）。
set -eu
HQ_HOME="$(cd "$(dirname "$0")/.." && pwd -P)"
T="$(mktemp -d /tmp/hq-e2e-codex.XXXXXX)"
T="$(cd "$T" && pwd -P)"
trap 'rm -rf "$T"' EXIT  # 无论结果如何都清理，保持采集环境干净
cd "$T"
git init -q && git config user.email e2e@hq && git config user.name e2e
printf 'def add(a, b):\n    return a + b\n' > app.py
printf '# e2e\n```hq-checks\nquick unit python3 -c "import app; assert app.add(2, 3) == 5"\n```\n' > ACCEPTANCE.md
sed -e 's/{name}/e2e/g; s/{theme}/测试/; s/{value}/学习/; s/{updated}/2026-01-01 00:00/' "$HQ_HOME/protocol/STATUS.template.md" > STATUS.md
printf '.hq/\n' > .gitignore
PYTHONPATH="$HQ_HOME" python3 -c "
from pathlib import Path; from hqlib import install
for p in ('.claude/settings.json', '.codex/hooks.json'): install.install_hook(Path('$T') / p)"
git add -A && git commit -qm init
echo "{\"cwd\":\"$T\",\"session_id\":\"seed\"}" | hq gate >/dev/null
codex exec --dangerously-bypass-hook-trust -c "projects.\"$T\".trust_level=\"trusted\"" \
  -c model_reasoning_effort=low --sandbox workspace-write --json \
  "给 app.py 增加一个 mul(a, b) 函数返回乘积。只做这件事，做完就结束。" < /dev/null > codex.jsonl 2>codex.err || true
if grep -q 'usage limit' codex.jsonl; then
  echo "NOT RUN: Codex 额度已用尽，本次无法验证闸门（不是闸门失败）。额度恢复后重跑。"
  exit 3
fi
echo "--- gate.log"; cat .hq/gate.log
fail=0
grep -q 'def mul' app.py || { echo "FAIL: agent 没有完成任务"; fail=1; }
grep -q 'block:status' .hq/gate.log || { echo "FAIL: 闸门没有因 STATUS 未更新而驳回"; fail=1; }
grep -q '	pass	' .hq/gate.log || { echo "FAIL: agent 更新 STATUS 后闸门没有放行"; fail=1; }
grep -q 'gave-up' .hq/gate.log && { echo "FAIL: 闸门放弃了"; fail=1; }
[ "$fail" = 0 ] && echo "OK: 驳回 → 更新 STATUS → 放行"
exit $fail
