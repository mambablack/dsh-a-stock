"""拉 A 股业绩报表（东财，季度全市场一次返回），存成本地面板。

数据用途：给策略提供**基本面**（ROE、营收/净利同比、毛利率），
用来验证质量因子，而不是只看价量。

时点纪律：以「最新公告日期」为可用日，回测时只能公告之后才可见。

用法：
    python scripts/fetch_financials.py                 # 2019Q1 至今
    python scripts/fetch_financials.py --start 2018-03-31
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "trader" / "data"
OUT = DATA / "financials.parquet"

COLUMN_MAP = {
    "股票代码": "code",
    "每股收益": "eps",
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "revenue_yoy",
    "净利润-净利润": "profit",
    "净利润-同比增长": "profit_yoy",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
    "每股经营现金流量": "ocf_ps",
    "所处行业": "em_industry",
    "最新公告日期": "announce_date",
}


def quarter_ends(start: str) -> list[str]:
    first = pd.Timestamp(start)
    last = pd.Timestamp.today()
    out = []
    year, month = first.year, 3
    while True:
        periods = ["0331", "0630", "0930", "1231"]
        for suffix in periods:
            stamp = pd.Timestamp(f"{year}{suffix}")
            if stamp < first:
                continue
            if stamp > last:
                return out
            out.append(stamp.strftime("%Y%m%d"))
        year += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-03-31")
    parser.add_argument("--sleep", type=float, default=0.5)
    args = parser.parse_args()

    import akshare as ak

    quarters = quarter_ends(args.start)
    existing = pd.read_parquet(OUT) if OUT.exists() else pd.DataFrame()
    done = set(existing["report_date"].astype(str).unique()) if not existing.empty else set()

    frames = []
    failures = []
    started = time.time()
    for index, quarter in enumerate(quarters, 1):
        report_date = f"{quarter[:4]}-{quarter[4:6]}-{quarter[6:]}"
        if report_date in done:
            continue
        try:
            frame = ak.stock_yjbb_em(date=quarter)
            if frame is None or frame.empty:
                failures.append((quarter, "empty"))
                continue
            frame = frame.rename(columns=COLUMN_MAP)
            keep = [c for c in COLUMN_MAP.values() if c in frame.columns]
            frame = frame[keep].copy()
            frame["report_date"] = report_date
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            frames.append(frame)
            print(f"[{index}/{len(quarters)}] {report_date}: {len(frame)} 只", flush=True)
        except Exception as exc:
            failures.append((quarter, type(exc).__name__))
            print(f"[{index}/{len(quarters)}] {report_date}: FAIL {type(exc).__name__}", flush=True)
        time.sleep(args.sleep)

    if frames:
        combined = pd.concat([existing, *frames], ignore_index=True) if not existing.empty else pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(["report_date", "code"])
        DATA.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(OUT, index=False)
        print(f"已保存 {OUT}: {combined.shape}，报告期 {combined['report_date'].nunique()} 个，"
              f"股票 {combined['code'].nunique()} 只，用时 {time.time()-started:.0f}s")
    else:
        print("没有新数据")
    if failures:
        print("失败:", failures[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())