# 给 Agent 的仓库说明

这个仓库是 A 股模拟盘交易员的工作区。你在这个仓库里工作时：

## 先读

1. `trader/memory/handover.md` —— 当前账户和打法状态；
2. `trader/memory/index.md` —— 有哪些知识、打法、待办；
3. `skills/a-share-trader/SKILL.md` —— 你的操作手册（开场协议、日循环、纪律）。

## 目录职责

| 目录 | 你能否写 | 说明 |
|---|---|---|
| `trader/memory/` | 能 | 知识、打法、日志；知识原文一字不改 |
| `trader/paper/` | 只能通过 `scripts/paper.py` | 直接改 JSON = 篡改账本 |
| `trader/plans/`、`trader/reports/` | 能 | 计划与报告 |
| `skills/` | **不能**（需人类批准） | 技能就是你的行为定义 |
| `scripts/` | 不能改 `paper.py` 的风控逻辑 | 需要变更先出提案 |
| `docs/` | 能 | 架构与机制说明 |

## 硬约束

- 只做模拟盘，不接券商；
- 不修改 `trader/paper/risk.json`；
- 没有止损价不许买入；
- 每笔交易必须有 `--reason`；
- 每日必须写 `trader/memory/journal/YYYY-MM-DD.md`；
- 改动打法必须走「候选 → 回测 → 纸面」流程。

## 与其他仓库的关系

- 回测：`C:\Users\wim\Desktop\codex\ashare-db\strategy-lab`（Python 在 `..\.venv\Scripts\python.exe`）；
- 行情：DSH 技能 `a-share-market-data`；
- 本仓库不含行情数据，只有交易记录与记忆。