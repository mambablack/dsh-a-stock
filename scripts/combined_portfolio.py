#!/usr/bin/env python3
"""两套打法的组合分析：相关性 + 不同权重下的组合表现。

对比对象
--------
- A：本仓库 PB-004（审美选股 v1.1e）的回测净值，读 `strategy-lab/runs/<run>/equity_daily.csv`
- B：ashare-db 的 core40 系列回测净值，读 `data/paper/paper.db` 的 equity 表

用法
----
    python scripts/combined_portfolio.py
    python scripts/combined_portfolio.py --run-a <run目录名> --run-b core40_rank@full
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]
RUNS = AHARE / "strategy-lab" / "runs"
LEGACY_DB = AHARE / "data" / "paper" / "paper.db"


def load_a(run: str | None) -> tuple[str, pd.Series]:
    if run is None:
        candidates = [p for p in RUNS.glob("aesthetic_v1e_*") if (p / "equity_daily.csv").exists()]
        if not candidates:
            raise SystemExit("找不到 PB-004 的回测目录")
        path = max(candidates, key=lambda p: p.stat().st_mtime)
    else:
        path = RUNS / run
    frame = pd.read_csv(path / "equity_daily.csv")
    date_col = "date" if "date" in frame.columns else frame.columns[0]
    value_col = "equity" if "equity" in frame.columns else frame.columns[-1]
    series = pd.Series(frame[value_col].to_numpy(dtype=float),
                       index=pd.to_datetime(frame[date_col])).sort_index()
    return path.name, series


def load_b(run_id: str) -> tuple[str, pd.Series]:
    con = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True, timeout=15)
    try:
        frame = pd.read_sql_query(
            "select date, equity from equity where run_id = ? order by date", con, params=(run_id,))
    finally:
        con.close()
    if frame.empty:
        raise SystemExit(f"legacy equity 里没有 run {run_id}")
    series = pd.Series(frame["equity"].to_numpy(dtype=float),
                       index=pd.to_datetime(frame["date"])).sort_index()
    return run_id, series


def metrics(series: pd.Series) -> dict[str, float]:
    daily = series.pct_change().dropna()
    years = max((series.index[-1] - series.index[0]).days / 365.25, 1e-9)
    total = series.iloc[-1] / series.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1
    mdd = float((series / series.cummax() - 1).min())
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return {"total": total, "cagr": cagr, "mdd": mdd, "sharpe": sharpe, "days": len(series)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组合分析")
    parser.add_argument("--run-a", default=None)
    parser.add_argument("--run-b", default="core40_rank@full")
    args = parser.parse_args(argv)

    name_a, a = load_a(args.run_a)
    name_b, b = load_b(args.run_b)
    print(f"A: {name_a}   {a.index[0].date()} .. {a.index[-1].date()}")
    print(f"B: {name_b}   {b.index[0].date()} .. {b.index[-1].date()}")

    ra, rb = a.pct_change(), b.pct_change()
    joined = pd.concat({"a": ra, "b": rb}, axis=1).dropna()
    corr = float(joined.corr().iloc[0, 1])
    print(f"\n日收益相关系数: {corr:.3f}  （重叠 {len(joined)} 个交易日）")

    print(f"\n{'组合':<10}{'总收益':>10}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}")
    for weight in (0.0, 0.3, 0.5, 0.7, 1.0):
        # 用重叠区间内的日收益合成，再归一成净值曲线
        series = (1 + weight * joined["b"] + (1 - weight) * joined["a"]).cumprod()
        m = metrics(series)
        label = f"{int((1-weight)*100)}/{int(weight*100)}"
        print(f"{label:<10}{m['total']*100:>9.1f}%{m['cagr']*100:>7.1f}%{m['mdd']*100:>7.1f}%{m['sharpe']:>8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())