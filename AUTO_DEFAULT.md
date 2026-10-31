# 五分钟默认决策

为 Codex、Qoder 和 ZCode 安装同一套本地 Stop hook。只有助手明确提出 A/B/C
选择题且恰好一个选项标注「最推荐」时才启动计时；用户回复会取消，满五分钟后
返回续跑指令，让助手采用推荐选项并记录结果。

## 安装与更新

需要 Python 3.11+。在克隆的 hq 仓库里运行：

```console
python bin/hq.py install-auto-default
python bin/hq.py install-auto-default --check
```

macOS/Linux 上也可用 `python3` 或已有的 `hq` 入口。只安装一个客户端：

```console
python bin/hq.py install-auto-default --agents zcode
```

安装器把当前 Python 和本仓库入口的绝对路径写入配置，因此不依赖 IDE 的 PATH
或当前工作目录。保留仓库目录；更新代码后重新执行安装命令即可。重复安装不重复
追加 hook，其他设置及第三方 hook 保持原样，首次修改前生成同目录 `.hq-backup`。
备份包含原始私人配置，不要提交。

| 客户端 | 用户级配置 | 执行方式 |
|---|---|---|
| Codex | `~/.codex/hooks.json` | command，330 秒 |
| Qoder | `~/.qoder/settings.json` | command，330 秒 |
| ZCode | `~/.zcode/cli/config.json` | process，330000 毫秒 |

每家安装 `Stop` 和 `UserPromptSubmit`，计时状态按客户端、会话和轮次隔离。
Codex 需审核信任变更后的 hook。ZCode 配置在会话启动时读取；安装后开启新会话。
如果 ZCode 的 hooks 已被显式禁用，安装器会报错，不会重新启用其他插件的 hook。

## 使用

明确要求助手在需要低风险选择时用以下格式提问：

```text
选择哪一种布局？
A. 保留当前布局
B. 简化布局（最推荐）
```

计时从 Stop hook 收到问题开始，而非从模型开始生成文字时开始。续跑提示为
`[HQ_AUTO_DEFAULT_TIMEOUT]`。日志只记录事件、会话标识和选项标签，不记录问题正文。

这是保守的格式/关键词筛选，不是完整的语义安全判断。删除、覆盖、发布、付款、
权限、凭据等风险词会阻止默认决策；续跑时助手仍必须遵守原有权限和审批规则。
没有 transcript、会话标识、唯一推荐项，或出现无法解析的 transcript 增量，
都不会自动选择。重复 Stop 和递归续跑不会重复应用同一默认值。

ZCode 的 transcript 是每次 hook 的临时快照；用户回复取消主要依赖
`UserPromptSubmit`。如果客户端未在等待期间派发该事件，仅有静态快照不能证明
用户未回复。安装检查与脚本测试不能替代客户端内的实际取消/续跑验收。

## 回归与验收

```console
python -m unittest discover -s tests -p test_autodefault.py -v
```

GitHub Actions 的 `Auto-default hooks` 在 macOS、Linux、Windows 上验证配置升级、
重复安装、已安装命令可运行、不同 transcript 格式、用户取消、风险拦截及重复事件。
可选真实五分钟测试（不调用模型）：

```sh
HQ_REALTIME_TEST=1 python3 -m unittest discover -s tests -p test_autodefault.py -v
```

客户端验收：在新会话中发起上面的低风险选择题，分别验证立即回复取消和无回复
满五分钟续跑。不要拿真实发布、删除或付款任务做测试。

## 接口依据

- Codex: https://developers.openai.com/codex/hooks
- Qoder: https://docs.qoder.com/extensions/hooks
- ZCode: https://zcode.z.ai/en/docs/hooks
