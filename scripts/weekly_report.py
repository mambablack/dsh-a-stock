#!/usr/bin/env python3
"""周报生成：账户、交易、打法健康度、本周提案、下周计划。

只读：读账本、净值、日志、提案，不改任何东西；输出一份 markdown 到
`trader/reports/WEEK-YYYY-Www.md`，并打印摘要供飞书卡片使用。

用法
----
    python scripts/weekly_report.py            # 最近一个自然周（周一~周日）
    python scripts/weekly_report.py --week 2026-11-16   # 指定周内的任意一天
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "trader" / "paper"
REPORTS = ROOT / "trader" / "reports"
JOURNAL = ROOT / "trader" / "memory" / "journal"
LEGACY_DB = ROOT.parents[1] / "data" / "paper" / "paper.db"


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def week_bounds(anchor: date) -> tuple[date, date]:
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6)


def account_summary(account: dict, start: date) -> dict:
    capital = float(account.get("capital", 0))
    cash = float(account.get("cash", 0))
    positions = account.get("positions") or {}
    value = cash + sum(float(p.get("units", 0)) * float(p.get("last_price") or p.get("avg_cost") or 0)
                       for p in positions.values())
    return {
        "capital": capital,
        "equity": value,
        "cash": cash,
        "positions": len(positions),
        "closed": int(account.get("closed_trades", 0)),
        "realized": float(account.get("realized_pnl", 0)),
    }


def nav_in_week(start: date, end: date) -> list[tuple[str, float]]:
    path = PAPER / "nav.csv"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                day = date.fromisoformat(parts[0])
            except ValueError:
                continue
            if start <= day <= end:
                rows.append((parts[0], float(parts[1])))
    return rows


def legacy_week(start: date, end: date) -> list[tuple[str, float]]:
    if not LEGACY_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "select date, equity from live_equity where date between ? and ? order by date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        con.close()
        return [(str(r[0]), float(r[1])) for r in rows]
    except sqlite3.Error:
        return []


def journals_in_week(start: date, end: date) -> list[Path]:
    out = []
    for path in sorted(JOURNAL.glob("*.md")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if start <= day <= end:
            out.append(path)
    return out


def proposals_in_week(start: date, end: date) -> dict[str, int]:
    counts = {"pending": 0, "approved": 0, "applied": 0, "rejected": 0}
    for path in REPORTS.glob("PROP-*.md"):
        text = path.read_text(encoding="utf-8")
        status_line = next((l for l in text.splitlines() if l.startswith("status:")), "status: pending")
        status = status_line.split(":", 1)[1].strip()
        day_line = next((l for l in text.splitlines() if l.startswith("date:")), "")
        try:
            day = date.fromisoformat(day_line.split(":", 1)[1].strip())
        except (ValueError, IndexError):
            continue
        if start <= day <= end and status in counts:
            counts[status] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="周报生成")
    parser.add_argument("--week", default=None, help="周内任意一天 YYYY-MM-DD")
    args = parser.parse_args(argv)

    anchor = date.fromisoformat(args.week) if args.week else date.today()
    start, end = week_bounds(anchor)
    iso_year, iso_week, _ = start.isocalendar()
    title = f"WEEK-{iso_year}-W{iso_week:02d}"

    account = load_json(PAPER / "account.json", {})
    summary = account_summary(account, start)
    my_nav = nav_in_week(start, end)
    legacy = legacy_week(start, end)
    journals = journals_in_week(start, end)
    props = proposals_in_week(start, end)

    week_change = ""
    if my_nav:
        first, last = my_nav[0][1], my_nav[-1][1]
        week_change = f"{last - first:+,.0f} 元（{(last / first - 1) * 100:+.2f}%）"

    lines = [
        f"# 周报 {title}（{start} ~ {end}）",
        "",
        "## 一、账户",
        "",
        f"- 权益 **{summary['equity']:,.0f} 元**（起始资金 {summary['capital']:,.0f}）",
        f"- 现金 {summary['cash']:,.0f}，持仓 {summary['positions']} 只，已实现盈亏 {summary['realized']:+,.0f}",
        f"- 本周净值变化：{week_change or '本周无净值记录'}",
        "",
        "## 二、净值",
        "",
        "| 日期 | 权益 |",
        "|---|---|",
    ]
    for day, value in my_nav:
        lines.append(f"| {day} | {value:,.0f} |")
    if not my_nav:
        lines.append("| — | — |")

    lines += ["", "## 三、对照（ashare-db core40）", ""]
    if legacy:
        lines += ["| 日期 | 权益 |", "|---|---|"]
        for day, value in legacy:
            lines.append(f"| {day} | {value:,.0f} |")
    else:
        lines.append("本周无数据（旧系统账本未更新或读不到）")

    lines += ["", "## 四、复盘与提案", "",
              f"- 本周日志 {len(journals)} 篇：{', '.join(p.stem for p in journals) or '无'}",
              f"- 提案：待批 {props['pending']} / 已批准 {props['approved']} / 已落实 {props['applied']} / 已驳回 {props['rejected']}",
              "", "## 五、下周计划", "",
              "- [ ] 检查数据管道（08:45 面板刷新是否正常）",
              "- [ ] 复核打法的滚动 20 笔期望",
              "- [ ] 有新的知识投喂就走 intake 流程"]

    out = REPORTS / f"{title}.md"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"周报: {out.relative_to(ROOT)}")
    print(f"权益 {summary['equity']:,.0f}（{week_change or '无变化'}），持仓 {summary['positions']} 只，日志 {len(journals)} 篇")
    print(f"提案：待批 {props['pending']} / 已批准 {props['approved']} / 已落实 {props['applied']} / 已驳回 {props['rejected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())