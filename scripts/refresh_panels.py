#!/usr/bin/env python3
"""盘前数据保鲜：把 ashare-db 的最新日线刷进 strategy-lab 面板。

背景：每日采集器（ashare-daily-update，18:00）把行情写进 `market.duckdb`，
但 strategy-lab 读的是 `data/panels` 下的 parquet 快照，两者不同步。
不刷新面板，盘前扫描就会用旧数据——宁可空仓，也不能拿旧数据下单。

退出码（给调用方判断，**不靠打印内容猜**）
----
    0  面板已是最新，或刷新成功
    2  读不到数据库最新日期（采集器持锁 / 表结构异常）——**数据不可用，当天不应交易**
    3  面板重建失败
    4  --check 模式下发现面板落后（需要刷新）

用法
----
    python scripts/refresh_panels.py            # 有更新就重建
    python scripts/refresh_panels.py --check    # 只看状态，不重建
    python scripts/refresh_panels.py --json     # 机器可读结果，给盘前例行用
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]                      # ashare-db
LAB = AHARE / "strategy-lab"
DB = AHARE / "data" / "market.duckdb"
PY = sys.executable
LOG = ROOT / "trader" / "logs" / "refresh_panels.log"

# 子进程只做一件事：读 daily 表的最大日期。写成文件执行，避免命令行引号地狱。
_PROBE_SOURCE = """\
import sys
import duckdb

path = sys.argv[1]
try:
    con = duckdb.connect(path, read_only=True)
except Exception as exc:                       # 采集器持锁时就是这里失败
    print("LOCKED " + repr(exc)[:300])
    raise SystemExit(0)

try:
    row = con.execute("select max(trade_date) from daily").fetchone()
except Exception as exc:
    print("SCHEMA " + repr(exc)[:300])
    raise SystemExit(0)
finally:
    con.close()

value = row[0] if row else None
print("OK " + (str(value)[:10] if value is not None else ""))
"""


def _log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


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


def db_latest(db_path: Path = DB) -> tuple[str | None, str]:
    """Return (latest_trade_date, status) where status is ok / locked / schema / error."""
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe.py"
        probe_path.write_text(_PROBE_SOURCE, encoding="utf-8")
        probe = subprocess.run([PY, str(probe_path), str(db_path)],
                               capture_output=True, text=True, encoding="utf-8", timeout=180)
    if probe.returncode != 0:
        return None, "error"
    lines = [line for line in probe.stdout.splitlines() if line.strip()]
    if not lines:
        return None, "error"
    head = lines[-1]
    if head.startswith("LOCKED"):
        return None, "locked"
    if head.startswith("SCHEMA"):
        return None, "schema"
    if head.startswith("OK "):
        value = head[3:].strip()
        return (value, "ok") if value else (None, "empty")
    return None, "error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="盘前面板刷新")
    parser.add_argument("--check", action="store_true", help="只检查，不重建")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--db", default=str(DB), help="数据库路径（测试用）")
    args = parser.parse_args(argv)

    start, end = panel_window()
    latest, status = db_latest(Path(args.db))

    if args.json:
        report = {"panel_start": start, "panel_end": end,
                  "db_latest": latest, "db_status": status, "action": "none"}
    else:
        print(f"面板窗口: {start} .. {end}")

    if status != "ok":
        reason = {
            "locked": "采集器正在写库（读锁冲突）",
            "schema": "daily 表不可用或字段异常",
            "empty": "daily 表没有数据",
        }.get(status, "探测失败")
        message = f"数据不可用：{reason}——当天不应交易"
        _log(f"unavailable ({status}); panels unchanged")
        if args.json:
            report.update({"ok": False, "message": message})
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(message)
        return 2

    if args.json:
        report["db_latest"] = latest
    else:
        print(f"库内最新日线: {latest}")

    if end and latest <= end:
        _log(f"panels already current at {end}")
        if args.json:
            report.update({"ok": True, "message": f"面板已是最新（{end}）"})
            print(json.dumps(report, ensure_ascii=False))
        else:
            print("面板已是最新，无需刷新")
        return 0

    if args.check:
        _log(f"stale: db={latest} > panel={end}")
        if args.json:
            report.update({"ok": False, "message": f"面板落后：库 {latest} vs 面板 {end}"})
            print(json.dumps(report, ensure_ascii=False))
        else:
            print("面板落后于数据库，需要刷新（去掉 --check 执行）")
        return 4

    _log(f"rebuilding panels: db={latest} > panel={end}")
    if not args.json:
        print("开始重建面板 ...")
    result = subprocess.run([PY, "-m", "strategy_lab", "data", "update"],
                            cwd=LAB, capture_output=True, text=True, encoding="utf-8")
    if not args.json:
        for line in (result.stdout or result.stderr).strip().splitlines()[-6:]:
            print("  " + line)
    if result.returncode != 0:
        _log(f"rebuild failed rc={result.returncode}")
        if args.json:
            report.update({"ok": False, "action": "failed", "message": "面板重建失败"})
            print(json.dumps(report, ensure_ascii=False))
        else:
            print("重建失败，盘前应跳过交易")
        return 3

    start, end = panel_window()
    _log(f"rebuilt: window now {start} .. {end}")
    if args.json:
        report.update({"ok": True, "action": "rebuilt", "panel_start": start, "panel_end": end,
                       "message": f"已重建面板到 {end}"})
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"刷新后面板窗口: {start} .. {end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())