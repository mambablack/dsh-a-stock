#!/usr/bin/env python3
"""完整交易日演练：把「买入 → 盯盘触发 → 卖出 → 对账」整条链跑一遍。

为什么需要：真实交易日里策略可能连续几天不出信号，交易腿（买/卖）就一直没被
验证过。这个脚本用确定性的价格序列，把与真实交易相同的命令链完整跑一遍，
任何一步失败都会报 FAIL。

用法
----
    python scripts/rehearse_day.py            # 在 data/rehearsal 下演练，不动真实账户
    python scripts/rehearse_day.py --keep     # 保留演练账户方便检查
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
PAPER = ROOT / "scripts" / "paper.py"
MONITOR = ROOT / "scripts" / "monitor.py"
REHEARSAL = ROOT / "data" / "rehearsal"

RESULTS: list[tuple[str, bool, str]] = []


def run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return result.returncode, (result.stdout + result.stderr).strip()


def step(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="完整交易日演练")
    parser.add_argument("--keep", action="store_true", help="保留演练账户")
    args = parser.parse_args(argv)

    REHEARSAL.mkdir(parents=True, exist_ok=True)
    account = REHEARSAL / "account.json"
    orders = REHEARSAL / "account.orders.jsonl"
    prices = REHEARSAL / "prices.json"
    alerts = REHEARSAL / "alerts.jsonl"
    for path in (account, orders, prices, alerts):
        path.unlink(missing_ok=True)

    # 1) 开盘：建仓（真实规则：单票 ≤12.5%、止损 −12%、止盈 +30%）
    code, out = run([PY, str(PAPER), "--account", str(account), "init", "--capital", "1000000"])
    step("账户初始化 100 万", code == 0, out.splitlines()[0] if out else "")

    code, out = run([PY, str(PAPER), "--account", str(account), "buy", "--code", "sz.000788",
                     "--units", "10000", "--price", "5.00", "--name", "北大医药",
                     "--reason", "演练：箱体突破", "--stop", "4.40", "--target", "6.50",
                     "--strategy", "PB-004"])
    step("买入 10,000 股 @5.00（PB-004，止损 4.40 / 止盈 6.50）", code == 0,
         out.splitlines()[0] if out else "")

    # 2) 把建仓日改成昨天，让 T+1 放行（真实交易日里第二天才能卖）
    payload = json.loads(account.read_text(encoding="utf-8"))
    for position in payload["positions"].values():
        position["entry_date"] = (date.today() - timedelta(days=1)).isoformat()
    account.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) 盘中：价格触及止盈，监控应报警
    prices.write_text(json.dumps({"sz.000788": 6.55}, ensure_ascii=False), encoding="utf-8")
    code, out = run([PY, str(MONITOR), "--once", "--anytime", "--account", str(account),
                     "--prices", str(prices), "--alerts", str(alerts), "--no-inject"])
    step("监控捕获止盈触发（6.55 ≥ 6.50）", code == 0 and "target" in out, out.splitlines()[-1] if out else "")
    step("告警落盘 alerts.jsonl", alerts.exists() and "target" in alerts.read_text(encoding="utf-8"))

    # 4) 卖出：按触发价成交
    code, out = run([PY, str(PAPER), "--account", str(account), "sell", "--code", "sz.000788",
                     "--units", "10000", "--price", "6.55", "--reason", "演练：止盈触发"])
    step("卖出 10,000 股 @6.55", code == 0, out.splitlines()[0] if out else "")

    # 5) 对账与状态
    code, out = run([PY, str(PAPER), "--account", str(account), "check"])
    step("对账通过", code == 0, out)

    code, out = run([PY, str(PAPER), "--account", str(account), "status"])
    equity_line = next((line for line in out.splitlines() if "权益" in line), "")
    ok = code == 0 and "1,0" in equity_line
    step("权益回到 100 万以上（止盈盈利）", ok, equity_line.strip())

    code, out = run([PY, str(PAPER), "--account", str(account), "history"])
    step("成交记录完整（买 1 笔 + 卖 1 笔 + 盈亏）", code == 0 and "sell" in out and "盈亏" in out)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n演练结果：{passed}/{len(RESULTS)} 项通过")
    if not args.keep:
        print(f"（演练账户保留在 {REHEARSAL.relative_to(ROOT)}，可随时删除）")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())