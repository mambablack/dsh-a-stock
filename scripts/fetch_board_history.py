"""一次性：拉东财概念板块日线历史，存成 parquet，供板块动量分析用。

用法：
    python scripts/fetch_board_history.py --start 2023-01-01
    python scripts/fetch_board_history.py --start 2023-01-01 --limit 50   # 先试小批量

输出：trader/data/board_history.parquet（board, date, close, pct_chg, amount）
断点续跑：已拉过的板块跳过（按 board 去重合并）。
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "trader" / "data"
OUT = DATA / "board_history.parquet"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.4)
    args = parser.parse_args()

    import akshare as ak

    boards = ak.stock_board_concept_name_em()
    names = boards["板块名称"].tolist()
    if args.limit:
        names = names[: args.limit]

    existing = pd.DataFrame()
    done: set[str] = set()
    if OUT.exists():
        existing = pd.read_parquet(OUT)
        done = set(existing["board"].unique())

    rows = []
    failures = []
    started = time.time()
    for index, name in enumerate(names, 1):
        if name in done:
            continue
        try:
            hist = ak.stock_board_concept_hist_em(
                symbol=name, period="daily",
                start_date=args.start.replace("-", ""), end_date="20500101", adjust="")
            if hist is None or hist.empty:
                failures.append((name, "empty"))
                continue
            frame = hist.copy()
            frame["board"] = name
            frame = frame.rename(columns={"日期": "date", "收盘": "close",
                                          "涨跌幅": "pct_chg", "成交额": "amount"})
            keep = [c for c in ("board", "date", "close", "pct_chg", "amount") if c in frame.columns]
            rows.append(frame[keep])
        except Exception as exc:
            failures.append((name, type(exc).__name__))
        time.sleep(args.sleep)
        if index % 50 == 0:
            elapsed = time.time() - started
            print(f"进度 {index}/{len(names)}  失败 {len(failures)}  用时 {elapsed:.0f}s", flush=True)

    if rows:
        combined = pd.concat([existing, pd.concat(rows, ignore_index=True)], ignore_index=True)
        combined = combined.drop_duplicates(["board", "date"]).sort_values(["board", "date"])
        DATA.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(OUT, index=False)
        print(f"已保存 {OUT}: {combined.shape}，板块 {combined['board'].nunique()} 个")
    else:
        print("没有新数据")
    if failures:
        print("失败样例:", failures[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())