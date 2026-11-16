#!/usr/bin/env python3
"""盘前数据保鲜：把 ashare-db 的最新日线刷进 strategy-lab 面板。

背景：每日采集器（ashare-daily-update）18:00 开始把行情写进 market.duckdb，
但 strategy-lab 读的是 data/panels 下的 parquet 快照，两者不是同一个东西。
不刷新面板，盘前扫描就会用旧数据——宁可空仓，也不能拿旧数据下单。

行为
----
1. 读 market.duckdb 的日线最大日期（只读；采集器在写时可能被锁，直接报告跳过）；
2. 读当前面板窗口；
3. 库里有更新的交易日 → 调用 ``python -m strategy_lab data update`` 重建面板；
4. 打印结果，供盘前例行判断能不能扫。

用法
----
    python scripts/refresh_panels.py            # 有更新就重建
    python scripts/refresh_panels.py --check    # 只看状态，不动
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]                      # ashare-db
LAB = AHARE / "strategy-lab"
DB = AHARE / "data" / "market.duckdb"
PY = sys.executable


def panel_window() -> tuple[str, str]:
    result = subprocess.run([PY, "-m", "strategy_lab", "data", "status"],
                            cwd=LAB, capture_output=True, text=True, encoding="utf-8")
    window = ""
    for line in result.stdout.splitlines():
        if line.strip().startswith("窗口"):
            window = line.split(":", 1)[-1].strip()
    if ".." in window:
        start, _, end = window.partition("..")
        return start.strip(), end.strip()
    return "", ""


def db_latest() -> str | None:
    """Latest trade date in the raw DB, or None when the collector holds the lock."""
    script = (
        "import duckdb,sys\n"
        "con=duckdb.connect(sys.argv[1], read_only=True)\n"
        "tables=[r[0] for r in con.execute(\"select table_name from information_schema.tables\").fetchall()]\n"
        "print(json.dumps(tables))\n"
    )
    probe = subprocess.run([PY, "-c", script, str(DB)], capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
    if probe.returncode != 0:
        return None
    try:
        tables = json.loads(probe.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    for table in ("daily", "daily_bars", "bars", "price_daily", "kline"):
        if table in tables:
            query = f"select max(trade_date) from {table}"
            out = subprocess.run([PY, "-c",
                                  f"import duckdb,sys;con=duckdb.connect(sys.argv[1], read_only=True);"
                                  f"print(con.execute(\"{query}\").fetchone()[0])", str(DB)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=120)
            if out.returncode == 0 and out.stdout.strip():
                return str(out.stdout.strip().splitlines()[-1])[:10]
    return None


LOG = ROOT / "trader" / "logs" / "refresh_panels.log"


def _log(message: str) -> None:
    """Append one line so the silent scheduled task leaves a trace."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="盘前面板刷新")
    parser.add_argument("--check", action="store_true", help="只检查，不重建")
    args = parser.parse_args(argv)

    start, end = panel_window()
    print(f"面板窗口: {start} .. {end}")

    latest = db_latest()
    if latest is None:
        print("库里读不到最新日期（采集器仍持有写锁，或表名未知）——按下面板旧数据判断")
        _log("db locked or unknown schema; panels unchanged")
        return 0
    print(f"库内最新日线: {latest}")
    if end and latest <= end:
        print("面板已是最新，无需刷新")
        _log(f"panels already current at {end}")
        return 0
    if args.check:
        print("面板落后于数据库，需要刷新（去掉 --check 执行）")
        return 0

    print("开始重建面板 ...")
    _log(f"rebuilding panels: db={latest} > panel={end}")
    result = subprocess.run([PY, "-m", "strategy_lab", "data", "update"],
                            cwd=LAB, capture_output=True, text=True, encoding="utf-8")
    tail = (result.stdout or result.stderr).strip().splitlines()[-6:]
    for line in tail:
        print("  " + line)
    if result.returncode != 0:
        print("重建失败，盘前应跳过交易")
        return result.returncode
    start, end = panel_window()
    print(f"刷新后面板窗口: {start} .. {end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())