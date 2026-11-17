# {name} · 验收标准

## 完成的定义

- （Henry 定：什么结果算“做完了”，用可观察的行为描述）

## 机器检查

格式：`档位 检查名 [timeout=秒] 命令`。`quick` 档每轮 Stop 闸门都跑（建议直接接入项目的 `just preflight` / `npm run lint && npm test` 等统一配方）；`full` 档由 `hq verify --tier full` 和夜间任务跑。

```hq-checks
quick  preflight  timeout=120  echo "TODO: 填入项目快检入口（如 just preflight 或 pytest/npm test）"
```

## UI / 交互实测口径（可选）

- 日常 Issue 验收优先采用 **80/20 局部靶向录屏**（跳过冷启动与前置跳转，只录目标交互短片），同时产出纯文本遥测与单页审阅卡。

## 只能人工验收的项（进 STATUS 的 ❓）

- （例如：真机物理光学、主观视觉审美、真实资金/外部授权）
