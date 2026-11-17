#!/usr/bin/env python3
"""开机自检：确认"换一个会话/压缩过上下文之后，该有的东西都还在"。

背景：交易员的记忆不在对话里，在文件里。但文件也可能过期、配置也可能互相矛盾。
这个脚本把所有"冷启动依赖"一次性核对，任何一条失败都说明**当前状态不可信**，
盘前应先修好再交易。

用法
----
    python scripts/doctor.py
    python scripts/doctor.py --json

退出码：0 全部通过 / 1 有失败项
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "trader" / "memory"
PAPER = ROOT / "trader" / "paper"
SKILL_DIR = Path.home() / ".dsh" / "skills" / "a-share-trader"
FEISHU_STATE = Path.home() / ".dsh" / "feishu-im" / "state.json"
TASKS = ("DSH-AShare-RefreshPanels", "DSH-AShare-Monitor")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交易员开机自检")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    # 1) 核心记忆文件
    for rel in ("handover.md", "index.md"):
        path = MEMORY / rel
        ok = path.exists() and path.stat().st_size > 200
        check(f"记忆文件 {rel}", ok, "" if ok else "缺失或过小")

    handover = MEMORY / "handover.md"
    if handover.exists():
        text = handover.read_text(encoding="utf-8")
        match = re.search(r"更新时间\**：(\d{4}-\d{2}-\d{2})", text)
        if match:
            age = (date.today() - date.fromisoformat(match.group(1))).days
            check("handover 新鲜度", age <= 3, f"{age} 天前" + ("（需要更新）" if age > 3 else ""))
        else:
            check("handover 新鲜度", False, "找不到更新时间")

    # 2) 账本
    account = read_json(PAPER / "account.json")
    if account is None:
        check("账户可读", False, "account.json 解析失败")
    else:
        cash = float(account.get("cash", 0))
        value = sum(float(p.get("units", 0)) * float(p.get("last_price") or p.get("avg_cost") or 0)
                    for p in (account.get("positions") or {}).values())
        check("账户可读", True, f"现金 {cash:,.0f} + 持仓 {value:,.0f}")
        check("现金非负", cash >= 0, f"{cash:,.0f}")

    # 3) 风控与分配一致
    risk = read_json(PAPER / "risk.json") or {}
    alloc = read_json(PAPER / "allocation.json") or {}
    active = [name for name, rule in (alloc.get("playbooks") or {}).items()
              if isinstance(rule, dict) and rule.get("status") in ("tracking", "verified")
              and float(rule.get("weight") or 0) > 0]
    check("有在跑的打法", len(active) > 0, ", ".join(active) if active else "没有 tracking/verified 且权重>0 的打法")
    for name in active:
        rule = alloc["playbooks"][name]
        slots = int(rule.get("max_positions") or 0)
        pct = float(rule.get("position_pct") or 0)
        ok = (0 < slots <= int(risk.get("max_positions", 0)) and
              pct <= float(risk.get("max_position_pct", 0)) * 1.005)
        check(f"分配与风控一致 {name}", ok,
              f"槽 {slots}/{risk.get('max_positions')}、单票 {pct:.3f}/{risk.get('max_position_pct')}")

    # 4) 当前策略文件
    handover_text = handover.read_text(encoding="utf-8") if handover.exists() else ""
    strategy_match = re.search(r"(aesthetic_v1e[^\s`）)]*)\.json", handover_text + " " +
                               (MEMORY / "index.md").read_text(encoding="utf-8") if (MEMORY / "index.md").exists() else handover_text)
    strategy_path = None
    lab = ROOT.parents[1] / "strategy-lab" / "strategies"
    for candidate in ("aesthetic_v1e.json",):
        path = lab / candidate
        if path.exists():
            strategy_path = path
    check("策略文件存在", strategy_path is not None,
          str(strategy_path.relative_to(ROOT.parents[1])) if strategy_path else "找不到 aesthetic_v1e.json")
    if strategy_path is not None:
        data = read_json(strategy_path) or {}
        slots = int((data.get("portfolio") or {}).get("max_positions") or 0)
        check("策略槽数与分配一致",
              all(int(alloc["playbooks"][n].get("max_positions") or 0) == slots for n in active),
              f"策略 {slots} 槽")

    # 5) 技能同步
    repo_skill = ROOT / "skills" / "a-share-trader" / "SKILL.md"
    if repo_skill.exists() and SKILL_DIR.exists():
        same = (repo_skill.read_bytes() == (SKILL_DIR / "SKILL.md").read_bytes())
        check("技能已同步到 DSH", same, "" if same else "repo 与 ~/.dsh/skills 不一致，跑 sync_skills.ps1")
    else:
        check("技能已同步到 DSH", False, "技能目录缺失")

    # 6) 飞书连接
    state = read_json(FEISHU_STATE)
    if state is None:
        check("飞书插件状态", False, "state.json 读不到")
    else:
        status = state.get("status") or {}
        connected = status.get("connection") == "connected"
        check("飞书插件连接", connected, status.get("lastError") or "")

    # 7) 计划任务（读任务定义文件，避免 schtasks 在受限进程里 RPC 失败）
    tasks_dir = Path(r"C:\Windows\System32\Tasks")
    for task in TASKS:
        ok = (tasks_dir / task).exists()
        check(f"计划任务 {task}", ok, "" if ok else "未注册")

    # 8) 今日是否有计划（交易日提醒，不算失败）
    today_plan = ROOT / "trader" / "plans" / f"{date.today().isoformat()}.md"
    check("今日计划文件", True, "已存在" if today_plan.exists() else "尚未生成（盘前例行会写）")

    failed = [name for name, ok, _ in RESULTS if not ok]
    if args.json:
        print(json.dumps({"ok": not failed, "failed": failed,
                          "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS]},
                         ensure_ascii=False))
    else:
        for name, ok, detail in RESULTS:
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))
        print(f"\n自检结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
        if failed:
            print("失败项：" + "、".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())