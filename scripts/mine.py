#!/usr/bin/env python3
"""挖掘批处理：批量回测一批候选策略，输出统一判定表。

为什么要有它
------------
挖掘的瓶颈不是"想不出点子"，而是"每个点子都要手动跑一遍、手动抄指标"。
这个脚本把「跑回测 → 收集指标 → 按统一标准判定」串成一条命令，
一次能过十几个假设，把精力留给想假设和读结果。

判定标准（与挖掘登记表一致）
--------------------------
- 笔数 ≥ 100
- 每笔净期望 > 0
- 组合 MDD ≤ 20%
- 完整性自检 PASS
（样本内外一致需要看报告，脚本只做初筛）

用法
----
    python scripts/mine.py --strategies squeeze_breakout,first_yin
    python scripts/mine.py --dir ../../strategy-lab/strategies --pattern "candidate_*"
    python scripts/mine.py --strategies a,b --reuse      # 用已有回测结果，不重跑
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AHARE = ROOT.parents[1]
LAB = AHARE / "strategy-lab"
RUNS = LAB / "runs"
REPORTS = ROOT / "trader" / "reports"
PY = sys.executable


def latest_run(strategy_id: str) -> Path | None:
    candidates = [p for p in RUNS.glob(f"{strategy_id}_*") if (p / "summary.json").exists()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def run_strategy(strategy_path: Path) -> Path | None:
    result = subprocess.run([PY, "-m", "strategy_lab", "run", str(strategy_path)],
                            cwd=LAB, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        print(f"  [FAIL] {strategy_path.name}: " + " | ".join(tail))
        return None
    found = latest_run(strategy_path.stem)
    return found


def summarize(run_dir: Path, name: str) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8-sig"))
    # 统计显著性看信号级样本（signal），组合表现看 portfolio
    trades = summary.get("signal") or summary.get("portfolio_trades") or {}
    portfolio = summary.get("portfolio") or {}
    validation = summary.get("validation_passed")
    return {
        "strategy": name,
        "trades": int(trades.get("n") or 0),
        "win": float(trades.get("win_rate") or 0) * 100,
        "expectancy": float(trades.get("expectancy") or 0) * 100,
        "cagr": float(portfolio.get("cagr") or 0) * 100,
        "mdd": float(portfolio.get("mdd") or 0) * 100,
        "sharpe": float(portfolio.get("sharpe") or 0),
        "integrity": "PASS" if validation else "FAIL",
    }


def verdict(row: dict) -> str:
    reasons = []
    if row["trades"] < 100:
        reasons.append(f"笔数不足({row['trades']})")
    if row["expectancy"] <= 0:
        reasons.append("期望为负")
    if row["mdd"] > 20:
        reasons.append(f"回撤超标({row['mdd']:.1f}%)")
    if row["integrity"] != "PASS":
        reasons.append("自检失败")
    return "通过初筛" if not reasons else "否决：" + "、".join(reasons)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量回测候选策略")
    parser.add_argument("--strategies", default="", help="逗号分隔的策略 id")
    parser.add_argument("--dir", default="", help="策略目录")
    parser.add_argument("--pattern", default="*.json")
    parser.add_argument("--reuse", action="store_true", help="用已有回测结果")
    args = parser.parse_args(argv)

    targets: list[Path] = []
    if args.strategies:
        for name in args.strategies.split(","):
            name = name.strip()
            if name:
                targets.append(LAB / "strategies" / f"{name}.json")
    if args.dir:
        targets.extend(sorted(Path(args.dir).glob(args.pattern)))
    if not targets:
        print("没有指定策略（--strategies 或 --dir）")
        return 2

    rows = []
    for path in targets:
        if not path.exists():
            print(f"  [SKIP] 不存在: {path}")
            continue
        name = path.stem
        print(f"[{name}] ...", flush=True)
        run_dir = latest_run(name) if args.reuse else None
        if run_dir is None:
            run_dir = run_strategy(path)
        if run_dir is None:
            continue
        rows.append(summarize(run_dir, name))

    if not rows:
        print("没有可汇总的结果")
        return 1

    header = f"{'策略':<24}{'笔数':>7}{'胜率':>7}{'每笔':>8}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}  判定"
    print("\n" + header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['strategy']:<24}{row['trades']:>7}{row['win']:>6.1f}%{row['expectancy']:>7.2f}%"
              f"{row['cagr']:>7.1f}%{row['mdd']:>7.1f}%{row['sharpe']:>8.2f}  {verdict(row)}")

    lines = ["| 策略 | 笔数 | 胜率 | 每笔期望 | CAGR | MDD | Sharpe | 判定 |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['strategy']} | {row['trades']} | {row['win']:.1f}% | {row['expectancy']:+.2f}% | "
                     f"{row['cagr']:.1f}% | {row['mdd']:.1f}% | {row['sharpe']:.2f} | {verdict(row)} |")
    REPORTS.mkdir(parents=True, exist_ok=True)
    from datetime import date
    out = REPORTS / f"mining-batch-{date.today().isoformat()}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())