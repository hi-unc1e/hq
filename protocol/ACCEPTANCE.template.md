# {name} · 验收标准

## 完成的定义

- （Henry 定：什么结果算“做完了”，用可观察的行为描述）

## 机器检查

格式：`档位 检查名 [timeout=秒] 命令`。`quick` 档每轮 Stop 闸门都跑（要快，< 1 分钟）；
`full` 档由 `hq verify --tier full` 和夜间任务跑。

```hq-checks
quick  unit  echo "TODO: 填入单测命令"
```

## 只能人工验收的项（进 STATUS 的 ❓）

- （例如：真机手感、视觉审美、需要管理员密码的安装）
