---
name: paper-trading
description: "模拟盘买卖与对账：用本地账本记录 A 股模拟交易（T+1、整手、佣金/印花税/过户费/滑点），执行风险闸门（单票上限、单日新开仓、回撤熔断），记录每日净值，输出持仓与历史成交。只在本地记账，不接入任何券商。"
whenToUse: "需要模拟买入/卖出、查看持仓与权益、更新持仓现价、记录每日净值、对账或回看成交记录时；每个交易日的开盘执行、盘中检查、收盘复盘都会用到。"
---

# 模拟盘账本 paper.py

脚本：`C:\Users\wim\Desktop\codex\ashare-db\agents\dsh-a-stock\scripts\paper.py`
账户：`trader/paper/account.json`　风险参数：`trader/paper/risk.json`（**人类可改，Agent 不可改**）

## 命令速查

```powershell
# 初始化（只跑一次；已有账户时加 --force 会先备份）
python scripts/paper.py init --capital 1000000

# 看账户（--json 给机器读）
python scripts/paper.py status

# 买入（模拟）：代码用 baostock 风格 sz.000788 / sh.600000
python scripts/paper.py buy --code sz.000788 --units 1000 --price 5.62 `
  --name 北大医药 --reason "箱体突破，PB-001 触发" --strategy aesthetic_v1 `
  --stop 4.95 --target 7.31

# 卖出（模拟）：--units 省略表示全部
python scripts/paper.py sell --code sz.000788 --units 1000 --price 5.90 --reason "触发止盈"

# 更新现价（--live 调行情 skill；或给 JSON 文件）
python scripts/paper.py mark --live
python scripts/paper.py mark --prices prices.json

# 记当日净值（收盘一次）
python scripts/paper.py nav --date 2026-11-16 --live

# 对账 / 成交记录
python scripts/paper.py check
python scripts/paper.py history --limit 20
```

## 规则（写死在代码里，别绕过）

- **T+1**：当日买入当日不能卖，`sell` 会拒绝；
- **整手**：股数必须是 100 的整数倍；
- **成本**：佣金万 2.5（最低 5 元）、印花税卖出 0.05%、过户费 0.001%、滑点 10bp 单边；
- **风险闸门**：单日新开 ≤2 笔、单票 ≤12.5%、持仓 ≤8 只、回撤 −15% 熔断；
- **越权**：只有人类明确要求时才可 `--force-human`，会被写进 `orders.jsonl` 的 `forced` 字段。

## 正确的调用顺序

**买入前**：
1. `quote.cmd quote <代码> --max-staleness 300` 拿现价，检查 `ok/market.is_trading/stale`；
2. 确认不是一字板/涨停开盘（现价 ≥ 涨停价就放弃）；
3. `paper.py buy ...` 带上 `--stop --target --reason`；
4. 如果拒单：**不要 --force-human**，把拒绝原因写进日志。

**卖出前**：
1. 取现价；
2. 判断触发的是止损/止盈/条件离场/时间到期；
3. `paper.py sell ... --reason "..."`。

**收盘**：
1. `mark --live`；2. `nav --live`；3. `check` 对账必须通过；4. 写 journal。

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 拒单「现金不足」 | 股价×股数+费用超过现金 | 减股数（按 100 股取整）或放弃 |
| 拒单「单票超过净值 12.5%」 | 仓位过大 | 按 12.5% 反算股数 |
| 拒单「今日新开仓已达上限 2」 | L2 限额 | 今天不再开新仓 |
| 拒单「回撤熔断」 | 账户回撤 >15% | 停止开新仓，飞书报告并等人类指示 |
| 「T+1 不能卖」 | 当天买的 | 明天再卖；当天触发止损只能记录 |

## 输出格式

`status` 会打印账户、持仓、今日新开笔数；Agent 汇报时引用其中的
**权益、回撤、持仓数、今日新开笔数** 四个数字，不要自己算。