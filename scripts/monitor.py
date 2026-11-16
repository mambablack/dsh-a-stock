#!/usr/bin/env python3
"""盘中监控：盯住持仓的止损/止盈，触发就通知交易台。

为什么需要
----------
routines 在 10:30 / 11:20 / 13:30 / 14:45 各检查一次；两次之间如果价格击穿止损，
靠"下一次例行"会慢 40-80 分钟。这个脚本补上中间的空档：每隔几分钟取一次价，
触发就把事件投进飞书交易台会话（复用插件的 inject 通道），由 Agent 按规则卖出。

用法
----
    python scripts/monitor.py --once                 # 跑一次（给 Windows 计划任务用）
    python scripts/monitor.py --loop --interval 180  # 常驻，每 3 分钟一次
    python scripts/monitor.py --once --dry-run        # 只打印，不注入
    python scripts/monitor.py --once --prices p.json  # 用给定价格测试

触发条件
--------
- 现价 <= 止损价：止损
- 现价 >= 止盈价：止盈
- 当日买入的票不卖（T+1），只提示"明日处理"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = ROOT / "trader" / "paper" / "account.json"
ALERTS = ROOT / "trader" / "paper" / "alerts.jsonl"
INJECT = Path.home() / ".dsh" / "feishu-im" / "inject.json"
QUOTE = Path.home() / ".dsh" / "skills" / "a-share-market-data" / "scripts" / "quote.cmd"
DEFAULT_CHAT = Path.home() / ".dsh" / "feishu-im" / "state.json"


def _load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _chat_id() -> str | None:
    state = _load(DEFAULT_CHAT, {}) or {}
    for key, link in (state.get("links") or {}).items():
        if key.startswith("p2p:") and isinstance(link, dict):
            return link.get("chatId")
    return None


def _prices_live(codes: list[str]) -> dict[str, float]:
    """Ask the quote skill for latest prices (one shot, no loop)."""
    if not QUOTE.exists():
        raise SystemExit(f"找不到行情脚本：{QUOTE}")
    raw = [code.split(".")[-1] for code in codes]
    result = subprocess.run(
        [str(QUOTE), "quote", *raw, "--max-staleness", "300"],
        capture_output=True, text=True, encoding="utf-8", shell=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"取价失败：{(result.stderr or result.stdout).strip()[:300]}")
    payload = json.loads(result.stdout)
    quotes = payload.get("quotes") or []
    out: dict[str, float] = {}
    for quote in quotes:
        code = str(quote.get("code", ""))
        market = str(quote.get("market", "")).lower()
        prefix = "sh" if market == "sh" else "sz" if market == "sz" else ""
        key = f"{prefix}.{code}" if prefix else code
        price = quote.get("price")
        if isinstance(price, (int, float)) and price > 0:
            out[key] = float(price)
            out[code] = float(price)
    return out


def _prices_file(path: Path) -> dict[str, float]:
    payload = _load(path, {}) or {}
    out: dict[str, float] = {}
    for code, value in payload.items():
        if isinstance(value, dict):
            value = value.get("price")
        if isinstance(value, (int, float)) and value > 0:
            out[str(code)] = float(value)
    return out


def check(account: dict[str, Any], prices: dict[str, float], today: str) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for code, position in (account.get("positions") or {}).items():
        price = prices.get(code)
        if price is None:
            continue
        stop, target = position.get("stop"), position.get("target")
        hit = None
        if isinstance(stop, (int, float)) and price <= stop:
            hit = "stop"
        elif isinstance(target, (int, float)) and price >= target:
            hit = "target"
        if hit is None:
            continue
        t1_blocked = position.get("entry_date") == today
        alerts.append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "name": position.get("name", ""),
            "units": position.get("units"),
            "price": price,
            "stop": stop,
            "target": target,
            "kind": hit,
            "t1_blocked": t1_blocked,
            "entry_date": position.get("entry_date"),
        })
    return alerts


def record(alerts: list[dict[str, Any]]) -> None:
    if not alerts:
        return
    ALERTS.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS.open("a", encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert, ensure_ascii=False) + "\n")


def inject(alerts: list[dict[str, Any]], chat_id: str | None) -> bool:
    if not alerts:
        return False
    lines = []
    for alert in alerts:
        action = "止损" if alert["kind"] == "stop" else "止盈"
        suffix = "（今日买入，T+1 不能卖，明日处理）" if alert["t1_blocked"] else ""
        lines.append(f"{alert['code']} {alert['name']} 现价 {alert['price']:.2f} "
                     f"触发{action}（止损 {alert['stop']} / 止盈 {alert['target']}）{suffix}")
    text = ("【盘中触发】" + "；".join(lines) +
            "。请按 a-share-trader 技能：能卖就 paper.py sell 并写 journal，"
            "T+1 不能卖就记录下来明天处理。不要新增买入。")
    stamp = datetime.now().strftime("%H%M%S")
    event = {
        "event_id": f"monitor-{stamp}",
        "sender": {"sender_id": {"open_id": "ou_monitor"}, "sender_type": "user"},
        "message": {
            "message_id": f"om_monitor_{stamp}",
            "chat_id": chat_id or "oc_monitor",
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }
    INJECT.parent.mkdir(parents=True, exist_ok=True)
    INJECT.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return True


def run_once(args: argparse.Namespace) -> int:
    account_path = Path(args.account) if getattr(args, "account", None) else ACCOUNT
    account = _load(account_path)
    if account is None:
        print("没有账户：先 python scripts/paper.py init")
        return 1
    positions = account.get("positions") or {}
    if not positions:
        print(f"{datetime.now():%H:%M:%S} 空仓，无需监控")
        return 0
    codes = list(positions)
    if args.prices:
        prices = _prices_file(Path(args.prices))
    else:
        prices = _prices_live(codes)
    missing = [code for code in codes if code not in prices]
    if missing:
        print(f"缺行情：{', '.join(missing)}")
    alerts = check(account, prices, date.today().isoformat())
    if not alerts:
        marks = "  ".join(f"{code}={prices.get(code, float('nan')):.2f}" for code in codes)
        print(f"{datetime.now():%H:%M:%S} 无触发  {marks}")
        return 0
    for alert in alerts:
        print(f"{alert['ts']} 触发 {alert['kind']}: {alert['code']} @ {alert['price']:.2f}")
    if args.dry_run:
        print("（dry-run：未写入 alerts、未注入事件）")
        return 0
    record(alerts)
    if args.inject:
        ok = inject(alerts, _chat_id())
        print("已注入飞书事件" if ok else "注入失败")
    return 0


def run_loop(args: argparse.Namespace) -> int:
    print(f"监控启动：间隔 {args.interval}s，Ctrl+C 停止")
    try:
        while True:
            try:
                run_once(args)
            except SystemExit as exc:
                print(f"本轮跳过：{exc}")
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"本轮异常：{exc}")
            time.sleep(max(30, args.interval))
    except KeyboardInterrupt:
        print("\n监控停止")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="持仓止损/止盈监控")
    parser.add_argument("--once", action="store_true", help="只跑一次")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--interval", type=int, default=180, help="循环间隔秒数（默认 180）")
    parser.add_argument("--prices", default=None, help="价格 JSON（测试用）")
    parser.add_argument("--account", default=None, help="账户文件（默认 trader/paper/account.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    parser.add_argument("--no-inject", dest="inject", action="store_false", default=True,
                        help="不向飞书注入事件")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.loop:
        return run_loop(args)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())