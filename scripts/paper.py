#!/usr/bin/env python3
"""模拟盘账本：A 股交易员的唯一事实来源。

设计原则
--------
1. **纯本地、纯记账**：不下单、不联网（除非显式 --live 取价），所有状态在
   `trader/paper/` 下的 JSON/JSONL 里；可以被 git 审计、可以被回放。
2. **规则与回测一致**：T+1、整手、佣金/印花税/过户费/滑点，和 strategy-lab 的
   回测引擎同一套口径，避免"回测赚钱、模拟盘亏钱"的口径漂移。
3. **风险闸门写死在代码里**：单票上限、单日新开仓数、持仓数、回撤熔断。Agent
   只能通过 `--force-human` 越过，并且会被记进 orders.jsonl 的 `forced` 字段。

常用命令
--------
    python scripts/paper.py init --capital 1000000
    python scripts/paper.py status [--json]
    python scripts/paper.py buy  --code sz.000788 --units 1000 --price 5.62 \
        --reason "箱体突破" --stop 4.95 --target 7.31
    python scripts/paper.py sell --code sz.000788 --units 1000 --price 5.90 --reason "止盈"
    python scripts/paper.py mark --live
    python scripts/paper.py mark --prices prices.json
    python scripts/paper.py nav --date 2026-11-16
    python scripts/paper.py check
    python scripts/paper.py history [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "trader" / "paper"
DEFAULT_ACCOUNT = PAPER / "account.json"
RISK_FILE = PAPER / "risk.json"
ORDERS = PAPER / "orders.jsonl"
NAV = PAPER / "nav.csv"
QUOTE_SKILL = Path(os.path.expanduser("~")) / ".dsh" / "skills" / "a-share-market-data" / "scripts" / "quote.cmd"
ALLOCATION = PAPER / "allocation.json"

DEFAULT_RISK = {
    "max_positions": 8,
    "max_new_per_day": 2,
    "max_position_pct": 0.125,
    "max_gross_exposure": 1.0,
    "lot_size": 100,
    "min_cash_pct": 0.0,
    "max_drawdown_stop": 0.15,
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "slippage": 0.001,
}


# ---------------------------------------------------------------------------
# io helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_risk() -> dict[str, Any]:
    risk = dict(DEFAULT_RISK)
    risk.update(_load_json(RISK_FILE, {}) or {})
    return risk


def _orders_path(args: argparse.Namespace) -> Path:
    account = _account_path(args)
    if account == DEFAULT_ACCOUNT:
        return ORDERS
    return account.with_name(account.stem + ".orders.jsonl")


def _nav_path(args: argparse.Namespace) -> Path:
    account = _account_path(args)
    if account == DEFAULT_ACCOUNT:
        return NAV
    return account.with_name(account.stem + ".nav.csv")


def _append_order(args: argparse.Namespace, record: dict[str, Any]) -> None:
    path = _orders_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _account_path(args: argparse.Namespace) -> Path:
    return Path(args.account) if getattr(args, "account", None) else DEFAULT_ACCOUNT


def _load_account(args: argparse.Namespace) -> dict[str, Any]:
    account = _load_json(_account_path(args))
    if account is None:
        raise SystemExit("还没有账户：先运行 python scripts/paper.py init --capital 1000000")
    return account


# ---------------------------------------------------------------------------
# costs (与 strategy-lab 回测口径一致)
# ---------------------------------------------------------------------------
def _buy_cost(amount: float, risk: dict[str, Any]) -> float:
    commission = max(amount * risk["commission_rate"], risk["commission_min"])
    transfer = amount * risk["transfer_fee_rate"]
    return commission + transfer


def _sell_cost(amount: float, risk: dict[str, Any]) -> float:
    commission = max(amount * risk["commission_rate"], risk["commission_min"])
    stamp = amount * risk["stamp_duty_rate"]
    transfer = amount * risk["transfer_fee_rate"]
    return commission + stamp + transfer


# ---------------------------------------------------------------------------
# valuation
# ---------------------------------------------------------------------------
def _equity(account: dict[str, Any]) -> float:
    value = 0.0
    for position in account["positions"].values():
        price = position.get("last_price") or position["avg_cost"]
        value += position["units"] * price
    return account["cash"] + value


def _peak_equity(account: dict[str, Any]) -> float:
    peak = account.get("peak_equity") or account["capital"]
    return max(peak, _equity(account))


def _today_new_positions(orders: Path) -> int:
    if not orders.exists():
        return 0
    today = date.today().isoformat()
    count = 0
    for line in orders.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("action") == "buy" and str(record.get("ts", "")).startswith(today) \
                and record.get("opened"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    path = _account_path(args)
    if path.exists() and not args.force:
        print(f"账户已存在：{path}（要重置加 --force，会备份旧文件）")
        return 1
    if path.exists():
        backup = path.with_suffix(f".bak-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(path, backup)
        print(f"旧账户已备份到 {backup.name}")
    capital = float(args.capital)
    account = {
        "version": 1,
        "account_id": args.account_id,
        "created": _now(),
        "capital": capital,
        "cash": capital,
        "peak_equity": capital,
        "positions": {},
        "closed_trades": 0,
        "realized_pnl": 0.0,
        "updated": _now(),
    }
    _save_json(path, account)
    if not RISK_FILE.exists():
        _save_json(RISK_FILE, DEFAULT_RISK)
    print(f"已初始化模拟账户：{capital:,.0f} 元  -> {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    account = _load_account(args)
    risk = _load_risk()
    positions = []
    for code, position in sorted(account["positions"].items()):
        price = position.get("last_price") or position["avg_cost"]
        value = position["units"] * price
        pnl = (price - position["avg_cost"]) * position["units"]
        positions.append({
            "code": code, "name": position.get("name", ""), "units": position["units"],
            "avg_cost": round(position["avg_cost"], 3), "last_price": round(price, 3),
            "value": round(value, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (position["avg_cost"] * position["units"]) * 100, 2)
            if position["units"] else 0.0,
            "entry_date": position.get("entry_date", ""), "stop": position.get("stop"),
            "target": position.get("target"), "strategy": position.get("strategy", ""),
        })
    equity = _equity(account)
    payload = {
        "account_id": account["account_id"],
        "capital": account["capital"],
        "cash": round(account["cash"], 2),
        "equity": round(equity, 2),
        "total_return_pct": round((equity / account["capital"] - 1) * 100, 2),
        "peak_equity": round(_peak_equity(account), 2),
        "drawdown_pct": round((equity / _peak_equity(account) - 1) * 100, 2),
        "n_positions": len(account["positions"]),
        "max_positions": risk["max_positions"],
        "new_today": _today_new_positions(_orders_path(args)),
        "max_new_per_day": risk["max_new_per_day"],
        "positions": positions,
        "updated": account["updated"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"账户 {payload['account_id']}   更新 {payload['updated']}")
    print(f"  资金 {payload['capital']:,.0f} -> 权益 {payload['equity']:,.0f} 元 "
          f"({payload['total_return_pct']:+.2f}%)   现金 {payload['cash']:,.0f}   "
          f"回撤 {payload['drawdown_pct']:.2f}%")
    print(f"  持仓 {payload['n_positions']}/{payload['max_positions']}   今日新开 "
          f"{payload['new_today']}/{payload['max_new_per_day']}")
    if positions:
        header = f"  {'代码':<12}{'名称':<10}{'股数':>8}{'成本':>8}{'现价':>8}{'市值':>12}{'盈亏':>10}{'止损':>7}{'止盈':>7}"
        print(header)
        for item in positions:
            print(f"  {item['code']:<12}{item['name'][:8]:<10}{item['units']:>8}"
                  f"{item['avg_cost']:>8.2f}{item['last_price']:>8.2f}{item['value']:>12,.0f}"
                  f"{item['pnl']:>10,.0f}{item['stop'] or 0:>7.2f}{item['target'] or 0:>7.2f}")
    else:
        print("  （空仓）")
    return 0


def _allocation() -> dict[str, Any]:
    """Per-playbook capital budget. Missing file means "no per-playbook limits"."""
    data = _load_json(ALLOCATION, {})
    return data if isinstance(data, dict) else {}


def _playbook_rule(strategy: str) -> dict[str, Any] | None:
    if not strategy:
        return None
    playbooks = _allocation().get("playbooks") or {}
    rule = playbooks.get(strategy)
    return rule if isinstance(rule, dict) else None


def _strategy_exposure(account: dict[str, Any], strategy: str) -> tuple[float, int]:
    """Cost basis and position count held under one playbook."""
    cost = 0.0
    count = 0
    for position in account["positions"].values():
        if (position.get("strategy") or "") == strategy:
            cost += position["units"] * position["avg_cost"]
            count += 1
    return cost, count


def _risk_check_buy(account: dict[str, Any], risk: dict[str, Any], code: str,
                    units: int, price: float, orders: Path,
                    strategy: str = "") -> tuple[bool, str]:
    if units <= 0 or units % risk["lot_size"] != 0:
        return False, f"股数必须是 {risk['lot_size']} 的整数倍"
    if code in account["positions"]:
        return False, "已持有该票，本策略不加仓"
    if len(account["positions"]) >= risk["max_positions"]:
        return False, f"持仓数已达上限 {risk['max_positions']}"
    if _today_new_positions(orders) >= risk["max_new_per_day"]:
        return False, f"今日新开仓已达上限 {risk['max_new_per_day']}"
    equity = _equity(account)
    cost = units * price
    if cost > account["cash"]:
        return False, f"现金不足（需要 {cost:,.0f}，可用 {account['cash']:,.0f}）"
    if cost > equity * risk["max_position_pct"]:
        return False, (f"单票超过净值 {risk['max_position_pct']*100:.1f}% "
                       f"（{cost:,.0f} > {equity * risk['max_position_pct']:,.0f}）")
    gross = sum(p["units"] * (p.get("last_price") or p["avg_cost"])
                for p in account["positions"].values()) + cost
    if gross > equity * risk["max_gross_exposure"]:
        return False, "总仓位超上限"
    drawdown = equity / _peak_equity(account) - 1
    if drawdown <= -risk["max_drawdown_stop"]:
        return False, (f"账户回撤 {drawdown*100:.1f}% 已触发熔断 "
                       f"({-risk['max_drawdown_stop']*100:.0f}%)，需人工确认")
    if account["cash"] - cost < equity * risk["min_cash_pct"]:
        return False, "现金低于最低保留比例"
    rule = _playbook_rule(strategy)
    if rule is not None:
        weight = float(rule.get("weight") or 0.0)
        if weight <= 0:
            return False, f"{strategy} 当前分配资金为 0（未上线/已退役）"
        used, held = _strategy_exposure(account, strategy)
        budget = equity * weight
        if used + cost > budget * 1.0001:
            return False, (f"{strategy} 资金预算不足：已用 {used:,.0f} + 新单 {cost:,.0f} "
                           f"> 预算 {budget:,.0f}（净值 {weight*100:.0f}%）")
        limit = int(rule.get("max_positions") or risk["max_positions"])
        if held >= limit:
            return False, f"{strategy} 持仓数已达上限 {limit}"
        position_cap = float(rule.get("position_pct") or risk["max_position_pct"])
        if cost > equity * position_cap * 1.0001:
            return False, (f"{strategy} 单票上限 {position_cap*100:.1f}% 净值"
                           f"（{cost:,.0f} > {equity * position_cap:,.0f}）")
    return True, ""


def cmd_buy(args: argparse.Namespace) -> int:
    account = _load_account(args)
    risk = _load_risk()
    price = float(args.price)
    units = int(args.units)
    ok, reason = _risk_check_buy(account, risk, args.code, units, price, _orders_path(args), args.strategy or "")
    if not ok and not args.force_human:
        print(f"拒单：{reason}")
        print("（确需执行请加 --force-human，会被记录为人工越权）")
        return 2
    amount = units * price
    fill_price = price * (1 + risk["slippage"])
    fee = _buy_cost(units * fill_price, risk)
    total = units * fill_price + fee
    if total > account["cash"] + 1e-6 and not args.force_human:
        print(f"拒单：含成本后现金不足（{total:,.0f} > {account['cash']:,.0f}）")
        return 2
    account["cash"] -= total
    account["positions"][args.code] = {
        "units": units, "avg_cost": (units * fill_price + fee) / units,
        "entry_date": date.today().isoformat(), "last_price": fill_price,
        "name": args.name or "", "strategy": args.strategy or "",
        "stop": args.stop, "target": args.target, "reason": args.reason,
    }
    account["updated"] = _now()
    account["peak_equity"] = _peak_equity(account)
    _save_json(_account_path(args), account)
    record = {
        "ts": _now(), "action": "buy", "code": args.code, "name": args.name or "",
        "units": units, "price": round(price, 3), "fill_price": round(fill_price, 4),
        "amount": round(amount, 2), "fee": round(fee, 2), "cash_after": round(account["cash"], 2),
        "opened": True, "reason": args.reason, "strategy": args.strategy or "",
        "stop": args.stop, "target": args.target, "forced": not ok,
        "risk_note": reason if not ok else "",
    }
    _append_order(args, record)
    print(f"买入 {args.code} {units} 股 @ {fill_price:.3f}  成本 {total:,.0f}（费用 {fee:.2f}）")
    print(f"现金余额 {account['cash']:,.0f}")
    return 0


def cmd_sell(args: argparse.Namespace) -> int:
    account = _load_account(args)
    risk = _load_risk()
    position = account["positions"].get(args.code)
    if position is None:
        print(f"没有持仓：{args.code}")
        return 2
    if position["entry_date"] == date.today().isoformat() and not args.force_human:
        print("T+1：今天买入的股票今天不能卖")
        return 2
    units = int(args.units) if args.units else position["units"]
    if units > position["units"]:
        print(f"卖出股数超过持仓（{units} > {position['units']}）")
        return 2
    price = float(args.price)
    fill_price = price * (1 - risk["slippage"])
    amount = units * fill_price
    fee = _sell_cost(amount, risk)
    proceeds = amount - fee
    account["cash"] += proceeds
    pnl = proceeds - position["avg_cost"] * units
    if units == position["units"]:
        del account["positions"][args.code]
        account["closed_trades"] = account.get("closed_trades", 0) + 1
    else:
        position["units"] -= units
        position["last_price"] = fill_price
    account["realized_pnl"] = account.get("realized_pnl", 0.0) + pnl
    account["updated"] = _now()
    account["peak_equity"] = _peak_equity(account)
    _save_json(_account_path(args), account)
    record = {
        "ts": _now(), "action": "sell", "code": args.code, "name": position.get("name", ""),
        "units": units, "price": round(price, 3), "fill_price": round(fill_price, 4),
        "amount": round(amount, 2), "fee": round(fee, 2), "cash_after": round(account["cash"], 2),
        "pnl": round(pnl, 2), "reason": args.reason, "strategy": position.get("strategy", ""),
        "forced": False,
    }
    _append_order(args, record)
    print(f"卖出 {args.code} {units} 股 @ {fill_price:.3f}  净得 {proceeds:,.0f}（费用 {fee:.2f}）")
    print(f"本笔盈亏 {pnl:+,.0f}   现金余额 {account['cash']:,.0f}")
    return 0


def _load_prices(args: argparse.Namespace, codes: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    if args.prices:
        payload = _load_json(Path(args.prices), {})
        for code, value in payload.items():
            if isinstance(value, dict):
                value = value.get("price")
            if value is not None:
                prices[code] = float(value)
        return prices
    if not args.live:
        return prices
    if not codes:
        return prices
    if not QUOTE_SKILL.exists():
        raise SystemExit(f"找不到行情脚本：{QUOTE_SKILL}")
    raw_codes = [code.split(".")[-1] for code in codes]
    command = [str(QUOTE_SKILL), "quote", *raw_codes, "--max-staleness", "300"]
    # Run the quote skill with this interpreter: the one on PATH may lack zoneinfo
    # timezone data (tzdata), which breaks the whole --live path.
    env = {**os.environ, "DSH_PYTHON": sys.executable}
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            shell=True, env=env)
    if result.returncode != 0:
        raise SystemExit(f"取价失败：{result.stderr.strip()[:400]}")
    payload = json.loads(result.stdout)
    for quote in payload.get("quotes", []):
        code = quote.get("code", "")
        market = (quote.get("market") or "").lower()
        prefix = "sh" if market == "sh" else "sz" if market == "sz" else ""
        key = f"{prefix}.{code}" if prefix else code
        prices[key] = float(quote["price"])
        prices[code] = float(quote["price"])
    return prices


def cmd_mark(args: argparse.Namespace) -> int:
    account = _load_account(args)
    codes = list(account["positions"].keys())
    if not codes:
        print("空仓，无需 mark")
        return 0
    prices = _load_prices(args, codes)
    missing = [code for code in codes if code not in prices]
    if missing:
        print(f"缺少行情：{', '.join(missing)}")
        if not args.allow_partial:
            return 2
    for code in codes:
        if code in prices:
            account["positions"][code]["last_price"] = prices[code]
    account["updated"] = _now()
    account["peak_equity"] = _peak_equity(account)
    _save_json(_account_path(args), account)
    print(f"已更新 {len(prices)} 个价格，权益 {_equity(account):,.0f}")
    return 0


def cmd_nav(args: argparse.Namespace) -> int:
    account = _load_account(args)
    if args.live or args.prices:
        codes = list(account["positions"].keys())
        prices = _load_prices(args, codes)
        for code in codes:
            if code in prices:
                account["positions"][code]["last_price"] = prices[code]
        _save_json(_account_path(args), account)
    equity = _equity(account)
    positions_value = equity - account["cash"]
    row = (f"{args.date or date.today().isoformat()},{equity:.2f},{account['cash']:.2f},"
           f"{positions_value:.2f},{len(account['positions'])}\n")
    nav_path = _nav_path(args)
    nav_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = args.date or date.today().isoformat()
    exists = nav_path.exists() and f"{stamp}," in nav_path.read_text(encoding="utf-8")
    if not nav_path.exists():
        with nav_path.open("w", encoding="utf-8") as handle:
            handle.write("date,equity,cash,positions_value,n_positions\n")
            handle.write(row)
    elif exists:
        print(f"{stamp} 已记录，跳过")
        return 0
    else:
        with nav_path.open("a", encoding="utf-8") as handle:
            handle.write(row)
    print(f"已记录净值：{row.strip()}")
    return 0


def cmd_alloc(args: argparse.Namespace) -> int:
    """Show each playbook's capital budget, usage and remaining room."""
    account = _load_account(args)
    equity = _equity(account)
    data = _allocation()
    playbooks = data.get("playbooks") or {}
    if not playbooks:
        print("没有分配表：所有策略共享全部资金（trader/paper/allocation.json 不存在）")
        return 0
    print(f"账户权益 {equity:,.0f} 元   现金 {account['cash']:,.0f} 元")
    print(f"{'策略':<8}{'状态':<12}{'权重':>6}{'预算':>12}{'已用':>12}{'可用':>12}{'持仓':>8}")
    total_weight = 0.0
    for name, rule in sorted(playbooks.items()):
        weight = float(rule.get("weight") or 0.0)
        total_weight += weight
        budget = equity * weight
        used, held = _strategy_exposure(account, name)
        limit = int(rule.get("max_positions") or 0)
        print(f"{name:<8}{str(rule.get('status', '?')):<12}{weight*100:>5.0f}%"
              f"{budget:>12,.0f}{used:>12,.0f}{budget-used:>12,.0f}{f'{held}/{limit}':>8}")
    print(f"权重合计 {total_weight*100:.0f}%（其余 {(1-total_weight)*100:.0f}% 视为现金缓冲）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    account = _load_account(args)
    equity = _equity(account)
    gross = sum(p["units"] * (p.get("last_price") or p["avg_cost"])
                for p in account["positions"].values())
    problems = []
    if account["cash"] < -1e-6:
        problems.append(f"现金为负：{account['cash']:.2f}")
    if abs((account["cash"] + gross) - equity) > 0.01:
        problems.append("现金 + 持仓市值 != 权益")
    for code, position in account["positions"].items():
        if position["units"] <= 0:
            problems.append(f"{code} 持仓股数非正")
        if not position.get("entry_date"):
            problems.append(f"{code} 缺少建仓日期")
    orders_path = _orders_path(args)
    if orders_path.exists():
        for line_no, line in enumerate(orders_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"orders.jsonl 第 {line_no} 行不是合法 JSON")
    if problems:
        print("对账失败：")
        for item in problems:
            print("  -", item)
        return 1
    print(f"对账通过：权益 {equity:,.2f} = 现金 {account['cash']:,.2f} + 持仓 {gross:,.2f}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    orders_path = _orders_path(args)
    if not orders_path.exists():
        print("还没有成交记录")
        return 0
    lines = [json.loads(line) for line in orders_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    for record in lines[-args.limit:]:
        pnl = record.get("pnl")
        tail = f"  盈亏 {pnl:+,.0f}" if pnl is not None else ""
        print(f"{record['ts']}  {record['action']:<4} {record['code']:<12}"
              f"{record['units']:>7} @ {record['fill_price']:<8.3f} {record.get('reason','')}{tail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A 股模拟盘账本")
    parser.add_argument("--account", default=None, help="账户文件（默认 trader/paper/account.json）")
    parser.add_argument("--json", action="store_true", help="status 输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化账户")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--account-id", default="paper-1")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("alloc", help="查看各打法资金分配与余量")
    p.set_defaults(func=cmd_alloc)

    p = sub.add_parser("status", help="查看账户")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("buy", help="买入（模拟）")
    p.add_argument("--code", required=True)
    p.add_argument("--units", type=int, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--name", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--strategy", default="")
    p.add_argument("--stop", type=float, default=None)
    p.add_argument("--target", type=float, default=None)
    p.add_argument("--force-human", action="store_true", help="人工越权，写进审计日志")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("sell", help="卖出（模拟）")
    p.add_argument("--code", required=True)
    p.add_argument("--units", type=int, default=0)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--force-human", action="store_true")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("mark", help="更新持仓现价")
    p.add_argument("--prices", default=None, help="JSON 文件 {code: price}")
    p.add_argument("--live", action="store_true", help="调用行情 skill 取价")
    p.add_argument("--allow-partial", action="store_true")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("nav", help="记录当日净值")
    p.add_argument("--date", default=None)
    p.add_argument("--prices", default=None)
    p.add_argument("--live", action="store_true")
    p.set_defaults(func=cmd_nav)

    p = sub.add_parser("check", help="对账")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("history", help="成交记录")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())