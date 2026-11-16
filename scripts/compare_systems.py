#!/usr/bin/env python3
"""两个模拟盘的对照：本仓库交易员 vs ashare-db 里那套 core40 实盘模拟。

两套系统各自 100 万、各自账本，互不干扰。这个脚本只读，不写任何账本，
用来回答一个问题：**钱交给哪套打法更有效率**。

用法
----
    python scripts/compare_systems.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MY_ACCOUNT = ROOT / "trader" / "paper" / "account.json"
LEGACY_DB = ROOT.parents[1] / "data" / "paper" / "paper.db"


def mine() -> dict | None:
    if not MY_ACCOUNT.exists():
        return None
    payload = json.loads(MY_ACCOUNT.read_text(encoding="utf-8"))
    capital = float(payload.get("capital", 0))
    equity = float(payload.get("cash", 0)) + sum(
        float(p.get("units", 0)) * float(p.get("last_price") or p.get("avg_cost") or 0)
        for p in (payload.get("positions") or {}).values()
    )
    return {
        "name": "dsh-a-stock（PB-004 审美 v1.1e）",
        "capital": capital,
        "equity": equity,
        "positions": len(payload.get("positions") or {}),
        "trades": int(payload.get("closed_trades", 0)),
        "updated": payload.get("updated", ""),
    }


def legacy() -> dict | None:
    if not LEGACY_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "select strategy, capital, cash, status, updated_at from live_accounts limit 1"
        ).fetchone()
        if row is None:
            return None
        equity_row = con.execute(
            "select date, equity, n_pos from live_equity order by date desc limit 1"
        ).fetchone()
        fills = con.execute("select count(*) from live_fills").fetchone()[0]
        strategy, capital, cash, status, updated = row
        return {
            "name": f"ashare-db（{strategy}）",
            "capital": float(capital),
            "equity": float(equity_row[1]) if equity_row else float(cash),
            "positions": int(equity_row[2]) if equity_row else 0,
            "trades": int(fills),
            "updated": updated,
        }
    except sqlite3.Error:
        return None
    finally:
        con.close()


def main() -> int:
    rows = [r for r in (mine(), legacy()) if r is not None]
    if not rows:
        print("两套账本都读不到")
        return 1
    print(f"{'系统':<32}{'起始':>12}{'当前权益':>14}{'收益':>9}{'持仓':>6}{'成交':>6}  更新")
    for row in rows:
        ret = row["equity"] / row["capital"] - 1 if row["capital"] else 0.0
        print(f"{row['name']:<32}{row['capital']:>12,.0f}{row['equity']:>14,.0f}"
              f"{ret*100:>8.2f}%{row['positions']:>6d}{row['trades']:>6d}  {row['updated']}")
    if len(rows) == 2:
        a, b = rows
        print()
        print(f"权益差：{b['name']} − {a['name']} = {b['equity'] - a['equity']:+,.0f} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())