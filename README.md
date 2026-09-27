# hq — 让 AI agent 用机器证据收工，让你只做判断

> **一句话**：`hq todo` 看所有项目里真正需要你拍板的事，`hq ok 1` 一条命令答复；
> agent 想带着没验证的改动收工？Stop 闸门直接把它拦回去。

**解决的核心问题**：AI coding agent 越来越能干，但"它说做完了"和"它真的做完了"之间
隔着一条信任鸿沟。你不可能逐个会话去复核，agent 也不可能每次都自证——hq 把这条
鸿沟变成三份文件和一个闸门，让"完成"重新变成可检验的东西。

## 1. 项目简介

hq 是一个跨项目的 agent 验收与看盘协议（Python 3.11+，零依赖）。每个接入的项目持有三份文件：

| 文件 | 谁写 | 作用 |
|---|---|---|
| `STATUS.md` | agent | 一句话现状、待你判断的 ❓、阻塞 ⛔、下一步 ▶ |
| `ACCEPTANCE.md` | 你 + agent | "完成的定义" + 机器检查（quick/full 两档命令） |
| `DECISIONS.md` | agent 代记 | 你确认过的取舍，防止同一个问题被反复问 |

CLI 五个人工命令（`--help` 里机器命令单独分组，不会混进来）：
`todo`（看盘/看详情）、`ok`（通过）、`note`（批注）、`status`（总览）、`brief`（简报）。
机器侧：`verify` 跑验收、`gate` 做 Stop hook、`init` 接入项目。

## 2. 核心痛点

- **"声称完成" ≠ 完成**：agent 汇报得很漂亮，测试是否真的绿、是否真的验收过，没人知道。
- **决定会蒸发**：你在某个会话里拍过板（"不用做多用户"），三天后另一个会话又来问一遍。
- **看盘成本爆炸**：五个项目十个会话，你不知道哪件事真的在等你，哪件是 agent 自己能推进的。
- **收工没有关卡**：会话结束就是结束了，没验证、没留痕、没下文。

## 3. 解决方案

三份文件定义"什么是完成、什么在等你"；CLI 让你的判断一条命令回流；Stop 闸门在
agent 试图结束时强制自证——quick 检查不过或 STATUS 没更新，直接驳回（同一轮最多
两次，防死循环；闸门自身异常一律放行，绝不卡死你的工作流）。

以下演示使用**虚构项目** `demo-api`（数据均为模拟）。

### 接入一个项目

```console
$ hq init ~/code/demo-api --name demo-api --theme 工具 --value 自用
• 创建 STATUS.md
• 创建 ACCEPTANCE.md
• 创建 DECISIONS.md
• 安装 Stop 闸门 → .claude/settings.json
• 安装 Stop 闸门 → .codex/hooks.json
• 登记到 hq.toml
```

之后 agent 在该项目的会话里收工时，闸门自动生效。

### 看盘：站在项目里，只看这个项目

```console
$ cd ~/code/demo-api && hq todo
❓ demo-api#1  错误响应要不要带 request_id
❓ demo-api#2  404 页面用 illustrations 库还是纯文字

答复：hq ok <编号> 通过；hq note <编号> "意见" 批注。
```

看某条的完整细节（怎么验 / 预期 / 建议，agent 写清楚了才许放进 ❓）：

```console
$ hq todo 1
❓ demo-api#1 · 错误响应要不要带 request_id   [待答]
   demo-api（工具 · 自用） · 更新 2026-09-27 10:00
   现状: v0.3 缓存层上线，P95 从 810ms 降到 240ms；404 页面还没做。

   怎么验：看一眼 v0.3 的错误示例，判断排障时是否真用得上
   预期：带上后用户报障能直接给定位线索
   建议：做，成本半小行代码

   文件: /code/demo-api/STATUS.md（❓ 第 15 行附近）
   答复: hq ok 1   或   hq note 1 "你的意见"
```

### 答复：一条命令，写回文件，agent 下轮自动消化

```console
$ hq ok 1
✅ - [x] **错误响应要不要带 request_id** — 怎么验：看一眼 v0.3 的错误示例 …
$ hq note 2 "用 illustrations 库，但只在 404 一处用，按需加载"
💬 - [ ] **404 页面用 illustrations 库还是纯文字** — … → Henry: 用 illustrations 库，但只在 404 一处用，按需加载
$ hq todo
没有待你判断的条目。（--all 查看全部项目）
```

（在项目目录里可省项目名：`hq ok 1`、`hq note 1 "…"`；跨项目写全 `hq ok demo-api#2`。）

### 闸门：agent 想收工，先过机器这一关

agent 改了代码但没更新 STATUS 就想结束会话（Stop hook 收到的 stdin JSON 由
Claude Code / Codex 自动供给）：

```console
$ echo '{"cwd":"/code/demo-api","session_id":"s1"}' | hq gate
{"decision": "block", "reason": "[hq 验收闸门] 代码有改动，但 /code/demo-api/STATUS.md
没有更新。按协议收工：1. front matter 的 updated；2. 一句话现状（结果，不是过程）；
3. ❓ 只放机器验证不了的事…"}

# agent 更新 STATUS 后再次结束：
{"decision": "block", "reason": "…quick 检查未通过…"}   # 若 ACCEPTANCE 检查失败
{}                                                      # 全部通过 → 放行
```

`hq verify [路径] [--tier quick|full]` 随时人工跑验收，结果写进 STATUS 的机器区
（✅ 区只由机器生成，手写不算证据）。

### 总览与简报

```console
$ hq status
demo-api       active  ❓0 ⛔0 ✅2/2  v0.3 缓存层上线，P95 从 810ms 降到 240ms；404 还没做。
weather-widget active  ❓1 ⛔0 ✅1/2  雨天动画偶发掉帧，待复验。
```

`hq brief` 生成早/晚报（待判断、验收异常、空转提醒），也可在 Markdown 里直接勾选
答复，下次生成前自动写回。

## 安装

```bash
git clone https://github.com/hi-unc1e/hq && cd hq
cp hq.toml.example hq.toml   # 登记你自己的项目（个人数据，已被 .gitignore 排除）
ln -s "$PWD/bin/hq" ~/bin/hq # 或任何 PATH 目录；需要 Python 3.11+
hq doctor                    # 自检
```

把 `hq gate` 配置为 Claude Code / Codex 的 Stop hook（`hq init` 会自动装），
闸门即生效。完整协议见 `protocol/PROTOCOL.md`。

## 设计原则

- **机器能证明的交给机器**（测试、指纹、复核），只有真判断才升给你；
- **闸门 fail-open**：验收工具自身出问题永远放行，不成为新的单点故障；
- **个人知识与工具分离**：登记表、决定、品味库全部 gitignore，仓库里只有机制。

## License

MIT
