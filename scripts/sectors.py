#!/usr/bin/env python3
"""行业板块数据：采集行业分类 + 每日板块强弱报告。

背景：本地库只有个股日线和 3 个宽基指数，没有行业/概念板块。复盘框架要求
"分析热点板块、定方向"，所以补两件事：

1. `fetch`：用 baostock 拉一次证监会行业分类（5,500+ 只，84 个行业），
   存 `trader/data/stock_industry.parquet`；行业分类变化慢，季度跑一次即可。
2. `report`：用本地面板的个股日线 + 行业映射，算每个行业的
   当日涨幅中位数、涨停家数、5 日涨幅、成交额，排出热门/冷门板块，
   输出 `trader/reports/sectors-YYYY-MM-DD.md`。

局限（要诚实）：证监会行业是"公司主业分类"，不等于市场炒的题材概念
（AI、机器人、固态电池这类）。题材板块需要另外采集东财概念数据，后续再加。

用法
----
    python scripts/sectors.py fetch
    python scripts/sectors.py report
    python scripts/sectors.py report --date 2026-11-16 --top 10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]
PANELS = AHARE / "strategy-lab" / "data" / "panels"
DATA = ROOT / "trader" / "data"
REPORTS = ROOT / "trader" / "reports"
INDUSTRY_FILE = DATA / "stock_industry.parquet"


def fetch_industry() -> int:
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        print(f"登录失败: {login.error_msg}")
        return 2
    rows: list[list[str]] = []
    try:
        result = bs.query_stock_industry()
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        fields = result.fields
    finally:
        bs.logout()

    frame = pd.DataFrame(rows, columns=fields)
    frame = frame[frame["industry"].str.strip() != ""]
    frame = frame[["code", "code_name", "industry"]].rename(
        columns={"code": "code", "code_name": "name", "industry": "industry"})
    DATA.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(INDUSTRY_FILE, index=False)
    print(f"行业分类已保存: {INDUSTRY_FILE}  股票 {len(frame)} 只 / 行业 {frame['industry'].nunique()} 个")
    print("更新日期:", date.today().isoformat())
    return 0


def _board(code: str) -> str:
    if code.startswith("sz.30"):
        return "chinext"
    if code.startswith("sh.68"):
        return "star"
    if code.startswith("bj."):
        return "bse"
    return "main"


def _panel(name: str) -> pd.DataFrame:
    frame = pd.read_parquet(PANELS / f"{name}.parquet")
    frame = frame.set_index("trade_date")
    frame.index = pd.to_datetime(frame.index)
    return frame


def report(target_date: str | None, top: int) -> int:
    if not INDUSTRY_FILE.exists():
        print(f"没有行业分类文件 {INDUSTRY_FILE}，先跑: python scripts/sectors.py fetch")
        return 2
    industry = pd.read_parquet(INDUSTRY_FILE).set_index("code")["industry"]

    close = _panel("close_adj")
    amount = _panel("amount")

    if target_date is None:
        target = close.index[-1]
    else:
        target = pd.Timestamp(target_date)
        if target not in close.index:
            print(f"{target.date()} 不在面板里（面板窗口 {close.index[0].date()} .. {close.index[-1].date()}）")
            return 2

    position = close.index.get_loc(target)
    if position < 6:
        print("面板历史不足 6 个交易日")
        return 2

    window = close.iloc[position - 5:position + 1]          # 6 天 → 5 日涨幅
    latest = window.iloc[-1]
    prev = window.iloc[-2]
    pct_1d = (latest / prev - 1.0) * 100
    pct_5d = (latest / window.iloc[0] - 1.0) * 100
    turnover_today = amount.loc[target]
    turnover_5d = amount.iloc[position - 5:position + 1].mean()

    valid = latest.notna() & prev.notna()
    rows = pd.DataFrame({
        "code": latest.index[valid],
        "pct_1d": pct_1d[valid].to_numpy(),
        "pct_5d": pct_5d[valid].to_numpy(),
        "amount": turnover_today[valid].to_numpy(),
    })
    rows["industry"] = rows["code"].map(industry)
    rows = rows[rows["industry"].notna()]

    # 涨停判定：用当日涨幅近似（主板 9.8%、创业板/科创板 19.7%）
    limit = np.where(rows["code"].str.startswith(("sz.30", "sh.68")), 19.5, 9.5)
    rows["limit_up"] = rows["pct_1d"] >= limit

    grouped = rows.groupby("industry").agg(
        股票数=("code", "count"),
        涨停家数=("limit_up", "sum"),
        涨幅中位=("pct_1d", "median"),
        五日涨幅中位=("pct_5d", "median"),
        成交额亿=("amount", lambda s: float(s.sum()) / 1e8),
    )
    grouped["涨停占比"] = grouped["涨停家数"] / grouped["股票数"]
    grouped = grouped.sort_values(["涨停家数", "涨幅中位"], ascending=False)

    total_limit = int(rows["limit_up"].sum())
    up = int((rows["pct_1d"] > 0).sum())
    down = int((rows["pct_1d"] < 0).sum())

    lines = [
        f"# 行业板块复盘 {target}",
        "",
        f"- 全市场：涨停 **{total_limit}** 家，上涨 {up} 家 / 下跌 {down} 家",
        f"- 样本：{len(rows)} 只（有行业分类且当日有数据）",
        "",
        f"## 热门行业 Top {top}（按涨停家数、涨幅中位数排序）",
        "",
        "| 行业 | 股票数 | 涨停 | 涨幅中位 | 5日涨幅中位 | 成交额(亿) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in grouped.head(top).iterrows():
        lines.append(f"| {name} | {int(row['股票数'])} | {int(row['涨停家数'])} | "
                     f"{row['涨幅中位']:+.2f}% | {row['五日涨幅中位']:+.2f}% | {row['成交额亿']:.0f} |")

    lines += ["", "## 冷门行业 Bottom 5", "",
              "| 行业 | 股票数 | 涨停 | 涨幅中位 | 5日涨幅中位 |",
              "|---|---:|---:|---:|---:|"]
    for name, row in grouped.tail(5).sort_values("涨幅中位").iterrows():
        lines.append(f"| {name} | {int(row['股票数'])} | {int(row['涨停家数'])} | "
                     f"{row['涨幅中位']:+.2f}% | {row['五日涨幅中位']:+.2f}% |")

    # 当前打法候选所在的行业（如果有）
    lines += ["", "## 备注", "",
              "- 涨停判定用涨幅近似（主板 ≥9.5%、双创 ≥19.5%），可能有 ±1 家误差；",
              "- 证监会行业 = 公司主业分类，不等于市场题材概念；概念板块数据后续补；",
              "- 用途：盘前判断「今天钱往哪个行业走」，避免在冷门行业里找票。"]

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"sectors-{target.date()}.md"
    out.write_text(text, encoding="utf-8")

    payload = {
        "date": str(target.date()),
        "market": {"limit_up": total_limit, "up": up, "down": down},
        "top": [{"industry": name, "limit_up": int(row["涨停家数"]),
                 "pct_1d": round(float(row["涨幅中位"]), 2),
                 "pct_5d": round(float(row["五日涨幅中位"]), 2)}
                for name, row in grouped.head(top).iterrows()],
    }
    print(text)
    print(f"报告: {out.relative_to(ROOT)}")
    print("JSON:", json.dumps(payload, ensure_ascii=False))
    return 0


def boards(top: int = 15) -> int:
    """东财题材/行业板块快照：一次调用拿到全部板块当日涨跌幅、涨跌家数、领涨股。

    这是"热门板块"的真口径（证监会行业分不出 AI/机器人这类题材）。
    盘前 09:15 运行时，快照就是上一交易日的板块表现。
    """
    import akshare as ak

    try:
        concepts = ak.stock_board_concept_name_em()
        industries = ak.stock_board_industry_name_em()
    except Exception as exc:                       # 网络/接口变动时明确失败
        print(f"[FAIL] 东财板块接口不可用: {type(exc).__name__}: {str(exc)[:200]}")
        return 2

    concepts = concepts.copy()
    concepts["板块类型"] = "概念"
    industries = industries.copy()
    industries["板块类型"] = "行业"
    merged = pd.concat([concepts, industries], ignore_index=True)

    DATA.mkdir(parents=True, exist_ok=True)
    snapshot_path = DATA / "boards_snapshot.parquet"
    merged.to_parquet(snapshot_path, index=False)

    hot = merged.sort_values("涨跌幅", ascending=False).head(top)
    cold = merged.sort_values("涨跌幅").head(5)

    lines = [
        f"# 板块快照 {date.today()}（东财口径）",
        "",
        f"- 概念 {len(concepts)} 个、行业 {len(industries)} 个",
        "",
        f"## 强势板块 Top {top}",
        "",
        "| 板块 | 类型 | 涨跌幅 | 上涨家数 | 下跌家数 | 领涨股 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for _, row in hot.iterrows():
        lines.append(f"| {row['板块名称']} | {row['板块类型']} | {row['涨跌幅']:+.2f}% | "
                     f"{int(row['上涨家数'])} | {int(row['下跌家数'])} | {row['领涨股票']} |")

    lines += ["", "## 弱势板块 Bottom 5", "",
              "| 板块 | 类型 | 涨跌幅 | 上涨家数 | 下跌家数 |",
              "|---|---|---:|---:|---:|"]
    for _, row in cold.iterrows():
        lines.append(f"| {row['板块名称']} | {row['板块类型']} | {row['涨跌幅']:+.2f}% | "
                     f"{int(row['上涨家数'])} | {int(row['下跌家数'])} |")

    lines += ["", "## 用法", "",
              "- 盘前看「钱在哪个题材」，只在这些板块所属的股票里找机会；",
              "- 与 `sectors.py report`（证监会行业，来自本地日线）互补：一个看题材，一个看行业。",
              "- 快照存 `trader/data/boards_snapshot.parquet`，可用 `ak.stock_board_concept_hist_em(period=\"daily\")` 补历史。"]

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"boards-{date.today().isoformat()}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"报告: {out.relative_to(ROOT)}")
    payload = {
        "date": date.today().isoformat(),
        "hot": [{"board": row["板块名称"], "type": row["板块类型"],
                 "pct": round(float(row["涨跌幅"]), 2),
                 "up": int(row["上涨家数"]), "down": int(row["下跌家数"])}
                for _, row in hot.head(10).iterrows()],
    }
    print("JSON:", json.dumps(payload, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行业板块数据与报告")
    parser.add_argument("action", choices=["fetch", "report", "boards"])
    parser.add_argument("--date", default=None)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if args.action == "fetch":
        return fetch_industry()
    if args.action == "boards":
        return boards(args.top)
    return report(args.date, args.top)


if __name__ == "__main__":
    raise SystemExit(main())