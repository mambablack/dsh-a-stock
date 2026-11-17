"""一次性：拉东财概念板块的成分股（板块 → 个股），供概念类策略使用。

用法：
    python scripts/fetch_board_members.py                 # 全量，断点续跑
    python scripts/fetch_board_members.py --limit 20      # 先试小批量
    python scripts/fetch_board_members.py --sleep 0.5     # 控制频率

输出：trader/data/board_members.parquet（board, code, name）
每个板块 ~8 秒，554 个约 1 小时；每 20 个板块存一次盘，中断后可续跑。
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "trader" / "data"
OUT = DATA / "board_members.parquet"


def save(rows: list[pd.DataFrame], existing: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in [existing, *rows] if frame is not None and not frame.empty]
    if not frames:
        return existing
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(["board", "code"])
    DATA.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUT, index=False)
    return combined


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--flush-every", type=int, default=20)
    args = parser.parse_args()

    import akshare as ak

    boards = ak.stock_board_concept_name_em()
    names = boards["板块名称"].tolist()
    if args.limit:
        names = names[: args.limit]

    existing = pd.read_parquet(OUT) if OUT.exists() else pd.DataFrame(columns=["board", "code", "name"])
    done = set(existing["board"].unique()) if not existing.empty else set()

    buffer: list[pd.DataFrame] = []
    failures: list[tuple[str, str]] = []
    started = time.time()
    processed = 0
    for index, name in enumerate(names, 1):
        if name in done:
            continue
        try:
            frame = ak.stock_board_concept_cons_em(symbol=name)
            if frame is None or frame.empty:
                failures.append((name, "empty"))
                continue
            frame = frame[["代码", "名称"]].rename(columns={"代码": "code", "名称": "name"})
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            frame["board"] = name
            buffer.append(frame[["board", "code", "name"]])
            processed += 1
        except Exception as exc:
            failures.append((name, type(exc).__name__))
        time.sleep(args.sleep)

        if len(buffer) >= args.flush_every:
            existing = save(buffer, existing)
            buffer = []
            elapsed = time.time() - started
            print(f"进度 {index}/{len(names)}  已存 {existing['board'].nunique()} 个板块 / {len(existing)} 行  "
                  f"失败 {len(failures)}  用时 {elapsed:.0f}s", flush=True)

    existing = save(buffer, existing)
    print(f"完成：{OUT}  {len(existing)} 行，板块 {existing['board'].nunique()} 个，失败 {len(failures)}")
    if failures:
        print("失败样例:", failures[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())