#!/usr/bin/env python3
"""回测结果深读：滚动收益、回撤结构、仓位暴露。

为什么需要
----------
summary.json 只给全周期 CAGR/MDD/Sharpe，但这些数字可能被某一年单独拉高。
这个脚本读 `equity_daily.csv` / `portfolio_daily.csv`，回答：
- 逐年表现（收益/回撤/夏普）；
- 滚动 1 年窗口的收益与夏普分布（最差的一年有多差）；
- 最大回撤发生在什么时候、持续多久；
- 平均仓位暴露（闲置资金比例）——衡量资金利用率。

用法
----
    python scripts/analyze_run.py --run aesthetic_v1e_g60x_20261117T205902
    python scripts/analyze_run.py --latest aesthetic_v1e
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]
RUNS = AHARE / "strategy-lab" / "runs"


def load_equity(run_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(run_dir / "equity_daily.csv")
    frame.columns = [c.strip().lower() for c in frame.columns]
    date_col = "date" if "date" in frame.columns else frame.columns[0]
    equity_col = "equity" if "equity" in frame.columns else frame.columns[-1]
    frame[date_col] = pd.to_datetime(frame[date_col])
    return frame.set_index(date_col)[[equity_col]].rename(columns={equity_col: "equity"})


def metrics(series: pd.Series) -> dict:
    daily = series.pct_change().dropna()
    if daily.empty:
        return {"total": 0.0, "cagr": 0.0, "mdd": 0.0, "sharpe": 0.0}
    years = max((series.index[-1] - series.index[0]).days / 365.25, 1e-9)
    total = series.iloc[-1] / series.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1
    mdd = float((series / series.cummax() - 1).min())
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return {"total": total, "cagr": cagr, "mdd": mdd, "sharpe": sharpe}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回测结果深读")
    parser.add_argument("--run", default="")
    parser.add_argument("--latest", default="", help="按前缀取最新一次回测")
    args = parser.parse_args(argv)

    if args.latest:
        candidates = [p for p in RUNS.glob(f"{args.latest}*") if (p / "equity_daily.csv").exists()]
        if not candidates:
            print(f"找不到 {args.latest} 的回测")
            return 2
        run_dir = max(candidates, key=lambda p: p.stat().st_mtime)
    elif args.run:
        run_dir = RUNS / args.run
    else:
        print("需要 --run 或 --latest")
        return 2

    equity = load_equity(run_dir)
    overall = metrics(equity["equity"])
    print(f"回测: {run_dir.name}")
    print(f"区间: {equity.index[0].date()} .. {equity.index[-1].date()}  ({len(equity)} 个交易日)")
    print(f"全周期: CAGR {overall['cagr']*100:.1f}%  MDD {overall['mdd']*100:.1f}%  Sharpe {overall['sharpe']:.2f}")

    print("\n逐年:")
    yearly = equity.groupby(equity.index.year)["equity"]
    print(f"{'年':<6}{'收益':>9}{'年内MDD':>10}{'夏普':>8}")
    for year, series in yearly:
        if len(series) < 20:
            continue
        m = metrics(series)
        print(f"{year:<6}{m['total']*100:>8.1f}%{m['mdd']*100:>9.1f}%{m['sharpe']:>8.2f}")

    # 滚动 252 日
    window = 252
    if len(equity) > window + 20:
        rolling_total = equity["equity"].pct_change(window).dropna()
        rolling_cagr = (1 + rolling_total) ** (252 / window) - 1
        print(f"\n滚动 {window} 日窗口（{len(rolling_cagr)} 个窗口）:")
        print(f"  收益: 最差 {rolling_cagr.min()*100:+.1f}% / 中位 {rolling_cagr.median()*100:+.1f}% / 最好 {rolling_cagr.max()*100:+.1f}%")
        print(f"  负收益窗口占比: {(rolling_cagr < 0).mean()*100:.1f}%")

    # 最大回撤区间
    drawdown = equity["equity"] / equity["equity"].cummax() - 1
    trough = drawdown.idxmin()
    before = equity.loc[:trough, "equity"]
    peak = before.idxmax()
    print(f"\n最大回撤: {drawdown.min()*100:.1f}%（{peak.date()} 峰值 → {trough.date()} 谷底，"
          f"{(trough - peak).days} 个自然日）")

    # 仓位暴露
    portfolio_file = run_dir / "portfolio_daily.csv"
    if portfolio_file.exists():
        frame = pd.read_csv(portfolio_file)
        frame.columns = [c.strip() for c in frame.columns]
        print("\nportfolio_daily 列:", list(frame.columns)[:10])
        for col in ("cash", "positions_value", "equity"):
            if col in frame.columns:
                values = pd.to_numeric(frame[col], errors="coerce")
                print(f"  {col}: 均值 {values.mean():,.0f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())