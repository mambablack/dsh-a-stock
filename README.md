# dsh-a-stock：A 股交易员 Agent

一个跑在本地的 A 股**模拟盘**交易员：用飞书或 DSH 对话框沟通，自主完成
选股 → 模拟买入 → 盯盘 → 模拟卖出 → 复盘，并把每一天的决定和每一次进化写进仓库。

> **只做模拟盘。** 没有券商接口，任何"下单"都只写本地账本。
> 输出的是"按规则该做什么"，不是投资建议。

## 它是什么

| 层 | 位置 | 说明 |
|---|---|---|
| 人格与流程 | `skills/a-share-trader/` | 开场协议、日循环、L2 限额、纪律 |
| 知识入库 | `skills/knowledge-intake/` + `scripts/intake.py` | 把你投喂的知识变成可检验的假设 |
| 模拟账本 | `scripts/paper.py` | T+1、整手、成本、风控闸门、净值、打法预算 |
| 盘中监控 | `scripts/monitor.py` | 止损/止盈触发 → 唤醒交易台 |
| 提案审批 | `scripts/propose.py` | 提案：新建 / 批准 / 落实 / 引用校验 |
| 完整链路演练 | `scripts/rehearse_day.py` | 买入→盯盘→卖出→对账，8 项断言 |
| 定时任务 | 飞书插件 `routines` | 盘前/盘中/收盘自动执行 |
| 记忆 | `trader/memory/` | 事实 / 知识 / 打法 / 日志 四层 |
| 回测引擎 | `../strategy-lab/` | 规则 DSL、回测、消融、敏感性 |
| 实时行情 | DSH 技能 `a-share-market-data` | 腾讯/东财/AkShare |

**会话只是 CPU，文件才是记忆。** 换会话、重启、`/new` 都不会让交易员失忆——
状态在 `trader/paper/`，知识在 `trader/memory/`。

## 当前状态（2026-11-17）

**打法 PB-004：审美选股 v1.1e**

| 项 | 值 |
|---|---|
| 选股 | 沪深主板+创业板、价格 5–8 元、流通市值 ≤40 亿、20 日成交额 ≥1000 万 |
| 条件 | 涨停基因 + 洗盘回撤 + 缩量 + 长影线 + MA20 不破 + 均线粘合/多头 + 箱体窄 + **箱体突破** + 非涨停收盘 |
| 闸门 | 全市场涨停家数 60 日分位 **≥50%** 才出手（冷市空仓） |
| 仓位 | 4 槽 × 25%，单日新开 ≤2 笔，回撤熔断 −22% |
| 卖出 | 止损 −12%、止盈 +30%、最长 10 个交易日 |
| 回测 | **CAGR 22.2% / MDD 10.8% / Sharpe 1.41**，2019–2026 八年全正，样本外每笔 +5.29% |

账户：模拟盘 100 万。交易台 = 飞书私聊；例行任务 09:15 / 10:30 / 11:20 / 13:30 / 14:45 / 15:05。

**两套模拟盘并行**：本仓库的 PB-004（100 万）+ ashare-db 里的 core40 月频排名（100 万）。
两者日收益相关系数 0.33，各 100 万合计 50/50 的组合回测 Sharpe 1.61、回撤 9.4%——
`scripts/combined_portfolio.py` 可以随时复算。

## 快速开始

```powershell
# 1) 初始化模拟账户（只跑一次）
python scripts\paper.py init --capital 1000000

# 2) 看账户
python scripts\paper.py status

# 2b) 看各打法资金预算与余量
python scripts\paper.py alloc

# 3) 把技能同步到 DSH（飞书会话据此加载）
powershell -ExecutionPolicy Bypass -File scripts\sync_skills.ps1
```

日常由 Agent 驱动：

- 飞书里说「盘前」→ 出今日计划；
- 说「我发你一段复盘，学一下」→ 走 knowledge-intake；
- 说「收盘复盘」→ 写日志 + 推卡片。

## 目录

```
trader/
├── memory/
│   ├── handover.md        当前状态（新会话第一件事）
│   ├── index.md           知识与打法索引
│   ├── knowledge/         你投喂的原文（一字不改）
│   ├── playbook/          打法卡（规则 + 证据 + 状态）
│   └── journal/           每日复盘
├── paper/                 账户、委托、净值（唯一事实）
├── plans/                 每日计划
└── reports/               周报/月报
scripts/
├── paper.py               模拟盘账本
└── sync_skills.ps1        技能同步到 ~/.dsh/skills
skills/                    交易员 / 知识入库 / 模拟交易 三段技能
docs/                      架构、手册、进化机制
```

## 三条铁律

1. **不接真实账户**，直到纸面跟踪验证通过（人类决定）；
2. **风险参数只有人类能改**（`trader/paper/risk.json`）；
3. **没有证据的知识不能变成交易规则**——所有新打法先进"候选"。

## 版本

- 2026-11-15：P0 落地（账本、记忆、技能、索引）