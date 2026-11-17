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


def momentum(top: int = 15) -> int:
    """概念板块动量面板：5/20/60 日涨幅、排名变化（轮动）、板块宽度。

    数据来自 `fetch_board_history.py` 拉下来的 `trader/data/board_history.parquet`。
    用途：盘前判断「资金在往哪个题材搬家」——只看当天涨幅会漏掉轮动趋势。
    """
    history_file = DATA / "board_history.parquet"
    if not history_file.exists():
        print(f"没有板块历史 {history_file}，先跑: python scripts/fetch_board_history.py")
        return 2
    history = pd.read_parquet(history_file)
    history["date"] = pd.to_datetime(history["date"])
    close = history.pivot_table(index="date", columns="board", values="close")
    close = close.sort_index()
    latest = close.index[-1]

    def ret(days: int) -> pd.Series:
        if len(close) <= days:
            return pd.Series(dtype=float)
        return (close.iloc[-1] / close.iloc[-1 - days] - 1.0) * 100

    r5, r20, r60 = ret(5), ret(20), ret(60)
    table = pd.DataFrame({"r5": r5, "r20": r20, "r60": r60}).dropna(subset=["r5", "r20"])
    if table.empty:
        print("板块历史太短，算不出动量")
        return 2

    # 剔除"事件类"板块（昨日涨停/连板/首板/打板/破净等），它们不是题材，只会污染排行
    event_keywords = ("昨日", "涨停", "连板", "首板", "打板", "含一字", "破净", "风格", "热股")
    table = table[~table.index.to_series().str.contains("|".join(event_keywords))]
    table["rank5"] = table["r5"].rank(ascending=False, pct=True)
    table["rank20"] = table["r20"].rank(ascending=False, pct=True)
    table["加速"] = table["rank20"] - table["rank5"]          # >0 表示近 5 日排名上升（在加速）
    table = table.sort_values("r5", ascending=False)

    breadth = float((table["r5"] > 0).mean() * 100)
    lines = [
        f"# 板块动量 {latest.date()}",
        "",
        f"- 覆盖 {len(table)} 个概念板块；近 5 日上涨的板块占 **{breadth:.0f}%**",
        "",
        f"## 近 5 日最强（Top {top}）",
        "",
        "| 板块 | 5日 | 20日 | 60日 |",
        "|---|---:|---:|---:|",
    ]
    for name, row in table.head(top).iterrows():
        lines.append(f"| {name} | {row['r5']:+.1f}% | {row['r20']:+.1f}% | {row['r60']:+.1f}% |")

    lines += ["", "## 近 20 日最强（Top 10）", "",
              "| 板块 | 20日 | 5日 | 60日 |", "|---|---:|---:|---:|"]
    for name, row in table.sort_values("r20", ascending=False).head(10).iterrows():
        lines.append(f"| {name} | {row['r20']:+.1f}% | {row['r5']:+.1f}% | {row['r60']:+.1f}% |")

    lines += ["", "## 正在加速的板块（5 日排名比 20 日排名明显靠前）", "",
              "| 板块 | 5日 | 20日 | 排名提升 |", "|---|---:|---:|---:|"]
    for name, row in table.sort_values("加速", ascending=False).head(10).iterrows():
        lines.append(f"| {name} | {row['r5']:+.1f}% | {row['r20']:+.1f}% | {row['加速'] * 100:.0f}pt |")

    lines += ["", "## 说明", "",
              "- 数据口径：东财概念板块指数（`stock_board_concept_hist_em`），2023 年至今；",
              "- 「加速」= 20 日排名分位 − 5 日排名分位，正值越大说明资金近期在切入；",
              "- 用途是**盘前背景**（知道钱在哪个方向），不是硬过滤条件（M-005 已证伪硬过滤）。"]

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"board-momentum-{latest.date()}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"报告: {out.relative_to(ROOT)}")
    return 0


def lineup(top_boards: int = 5, only_pool: bool = True) -> int:
    """热点题材 → 成分股名单。

    回答盘前最实际的问题：**今天最强的几个题材里，有哪些票在我们能买的池子里**
    （默认池子 = PB-004 的 5–8 元、主板/创业板、非 ST）。

    板块热度来自 `board_history.parquet` 的 5 日动量；
    个股数据来自 strategy-lab 面板（最新一天，若与板块日期不一致会提示）。
    """
    history_file = DATA / "board_history.parquet"
    members_file = DATA / "board_members.parquet"
    if not history_file.exists() or not members_file.exists():
        print("缺数据：先跑 fetch_board_history.py 和 fetch_board_members.py")
        return 2

    history = pd.read_parquet(history_file)
    history["date"] = pd.to_datetime(history["date"])
    close = history.pivot_table(index="date", columns="board", values="close").sort_index()
    board_date = close.index[-1]
    ret5 = (close.iloc[-1] / close.iloc[-6] - 1.0) * 100

    event_keywords = ("昨日", "涨停", "连板", "首板", "打板", "含一字", "破净", "风格", "热股")
    ret5 = ret5[~ret5.index.to_series().str.contains("|".join(event_keywords))]
    hot = ret5.sort_values(ascending=False).head(top_boards)

    def panel_code(code: str) -> str:
        code = str(code).zfill(6)
        if code.startswith(("6", "9")):
            return "sh." + code
        if code.startswith(("0", "2", "3")):
            return "sz." + code
        return "bj." + code

    members = pd.read_parquet(members_file)
    panels = PANELS
    price = pd.read_parquet(panels / "close_raw.parquet").set_index("trade_date")
    price.index = pd.to_datetime(price.index)
    pct = (price.iloc[-1] / price.iloc[-2] - 1.0) * 100
    stock_date = price.index[-1]

    lines = [f"# 热点题材成分股 {board_date.date()}",
             "",
             f"- 板块数据：{board_date.date()}；个股数据：{stock_date.date()}"
             + ("（**日期不一致，盘前请先刷新面板**）" if stock_date.date() != board_date.date() else ""),
             ""]
    for board, board_ret in hot.items():
        codes = [panel_code(x) for x in members.loc[members["board"] == board, "code"].tolist()]
        if not codes:
            continue
        frame = pd.DataFrame({"code": codes,
                              "price": [price.at[stock_date, c] if c in price.columns else float("nan")
                                        for c in codes],
                              "pct": [pct.get(c, float("nan")) for c in codes]})
        frame = frame.dropna(subset=["price"])
        if frame.empty:
            continue
        if only_pool:
            frame = frame[(frame["price"] >= 5.0) & (frame["price"] <= 8.0)]
        frame = frame.sort_values("pct", ascending=False)
        lines.append(f"## {board}（5日 {board_ret:+.1f}%）—— 池内 {len(frame)} 只")
        lines.append("")
        if frame.empty:
            lines.append("（池子里没有成分股）")
        else:
            lines.append("| 代码 | 价格 | 当日涨跌 |")
            lines.append("|---|---:|---:|")
            for _, row in frame.head(12).iterrows():
                lines.append(f"| {row['code']} | {row['price']:.2f} | {row['pct']:+.2f}% |")
        lines.append("")

    lines += ["## 说明", "",
              "- 池子口径：价格 5–8 元（PB-004 的价格带），不含 ST/退市过滤（还要看策略扫描结果）；",
              "- 用途：盘前知道「热点题材里有没有我们的票」，配合 `sector report` 与策略扫描一起看。"]

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"lineup-{board_date.date()}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"报告: {out.relative_to(ROOT)}")
    return 0


def check(codes_text: str) -> int:
    """查候选票属于哪些题材、这些题材今天/近 5 日强不强。

    用法：python scripts/sectors.py check --codes sz.000404,sh.600998
    盘前扫描出候选后，用它给每只票补一条"所属题材与热度"的判断依据。
    """
    members_file = DATA / "board_members.parquet"
    history_file = DATA / "board_history.parquet"
    if not members_file.exists():
        print("缺 board_members.parquet，先跑 fetch_board_members.py")
        return 2

    def normalize(code: str) -> str:
        code = code.strip().lower()
        if "." in code:
            code = code.split(".")[1]
        return code.zfill(6)

    wanted = [normalize(c) for c in codes_text.split(",") if c.strip()]
    members = pd.read_parquet(members_file)
    members["code"] = members["code"].astype(str).str.zfill(6)

    momentum_map: dict[str, tuple[float, float]] = {}
    if history_file.exists():
        history = pd.read_parquet(history_file)
        history["date"] = pd.to_datetime(history["date"])
        close = history.pivot_table(index="date", columns="board", values="close").sort_index()
        r5 = (close.iloc[-1] / close.iloc[-6] - 1.0) * 100
        r20 = (close.iloc[-1] / close.iloc[-21] - 1.0) * 100
        momentum_map = {board: (float(r5[board]), float(r20[board])) for board in r5.index}

    for code in wanted:
        boards = members.loc[members["code"] == code, "board"].tolist()
        if not boards:
            print(f"{code}: 没有题材数据（可能不在东财概念里）")
            continue
        rows = []
        for board in boards:
            r5, r20 = momentum_map.get(board, (float("nan"), float("nan")))
            rows.append((board, r5, r20))
        rows.sort(key=lambda item: (-(item[1] if item[1] == item[1] else -99)))
        print(f"\n{code}（{len(rows)} 个题材，按 5 日动量排序）")
        for board, r5, r20 in rows[:8]:
            tag = "🔥" if r5 > 3 else ("❄️" if r5 < -3 else "")
            print(f"  {board:<16} 5日 {r5:+6.1f}%  20日 {r20:+6.1f}%  {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行业板块数据与报告")
    parser.add_argument("action", choices=["fetch", "report", "boards", "momentum", "lineup", "check"])
    parser.add_argument("--date", default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--codes", default="")
    args = parser.parse_args(argv)
    if args.action == "fetch":
        return fetch_industry()
    if args.action == "boards":
        return boards(args.top)
    if args.action == "momentum":
        return momentum(args.top)
    if args.action == "lineup":
        return lineup(max(1, min(args.top, 10)))
    if args.action == "check":
        if not args.codes:
            print("用法: sectors.py check --codes sz.000404,sh.600998")
            return 2
        return check(args.codes)
    return report(args.date, args.top)


if __name__ == "__main__":
    raise SystemExit(main())