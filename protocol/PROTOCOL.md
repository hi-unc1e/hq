# henry-hq 项目协议 v1

目的：让 Henry **只看需要他判断的东西**，其余由机器证明或由 agent 自主推进；三份文件采用**深度融合的记忆体**写法，不做越堆越长的 CRUD 流水账。

接入协议的项目，根目录有三份文件：

| 文件 | 谁写 | 给谁看 | 作用 |
|---|---|---|---|
| `STATUS.md` | agent 替换更新 | Henry | 一屏现状摘要 + 待判断清单（人工区 `<= 75` 行） |
| `ACCEPTANCE.md` | Henry 定标准，agent 挂检查 | agent / 机器 | 完成定义 + `hq-checks`（优先复用 `just` / `npm` 等统一门禁） |
| `DECISIONS.md` | agent 融合维护，Henry 裁决 | agent | 按领域提炼的生效决定（**开工必读，不许回退**） |

`.hq/`（已加入 `.gitignore`）存放机器产物：`verify.json`、检查日志、闸门状态；过时原文可归档至 `.hq/history/`，移出日常上下文。

## 开工

1. 读 `STATUS.md`、`DECISIONS.md`、`ACCEPTANCE.md`（不读 `.hq/history/` 归档全文）。
2. 处理 `❓` 里 Henry **已勾选或已批注**的条目：
   - 批注含长期取舍 → **融合**进 `DECISIONS.md` 对应领域小节（必要时附上旧编号溯源），不无限追加日期条目；
   - 随即从 `❓` 移除已闭环项，保持清单清爽。

## 收工（Stop 闸门会检查）

1. 跑 `hq verify`（默认 `quick` 档；交付性改动跑 `hq verify --tier full`）。
   **不要自己往 ✅ 区写“已通过”**——那一块由 `hq verify` 生成，agent 写的不算证据。
2. 替换更新 `STATUS.md`（不追加历史日报）：
   - front matter 的 `updated`；
   - 第一条引用行 `> ...`：**纯文本结果摘要（`<= 60` 字、勿带 Markdown 链接/长 URL 与冗余前缀）**，确保 `hq status` 单行清晰展示不截断；
   - `❓ 待 Henry 判断`：只放**机器验证不了、必须真人的眼睛/耳朵/手/品味/权限**的事，格式 `- [ ] <事项> — 怎么验：… — 预期：… — 建议：…`；
   - `⛔ 阻塞`：只留当前真实有效阻塞；
   - `▶ 下一步`：你将自主推进的事，**不需要 Henry 回复**。
3. 技术教训优先通过 `add-rule` / `<200ms` 静态检查固化到代码与 Git Hook，不往文档堆口头叮嘱。

## Stop 闸门（跨 Harness 自动执行）

agent 试图结束一轮时，项目 `.claude/settings.json` / `.codex/hooks.json` 先执行 `<200ms` 确定性不变量与工作区检查（如代码红线、文档防膨胀），再运行**单次** `hq gate`：

1. 代码自上次通过后有变更 → 跑 `quick` 检查；失败则**驳回结束**，把失败输出交还 agent。
2. 代码有变更但 `STATUS.md`（除机器区外）没更新 → **驳回结束**，要求按上文更新。
3. 同一轮最多驳回 2 次，之后放行并在 `.hq/gate.log` 记一笔；闸门自身异常始终 fail-open。

## Henry 怎么验收

- 看晨报/晚报（`hq brief`）、`hq status` 或 `hq todo -i` 交互看板。
- UI/交互改动优先审阅 **80/20 局部靶向短片 + 单页 `review.html` + `agent-digest.md`**，不人肉翻长视频或千行源码。
- 在 `STATUS.md` 或 `hq ok` / `hq note` 中答复；不需要再说「继续」——`▶ 下一步` 就是默认授权。
