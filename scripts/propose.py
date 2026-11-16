#!/usr/bin/env python3
"""提案卡：让"进化"有审批、有版本、有记录。

Agent 在周度自评里发现打法衰减，或用户投喂的新知识通过回测后，都要先出一张
提案卡，人类回「批准/驳回」才落地。本脚本只管建档与状态流转，不自动改任何策略。

命令
----
    python scripts/propose.py new --title "收紧箱体宽度" --run <run目录> \
        --change "把 box_width_10 从 0.18 收紧到 0.14" \
        --impact "预计信号减少 30%，期望从 +3.40% 到 +2.9%（回测）"
    python scripts/propose.py list
    python scripts/propose.py decide --id PROP-20261116-01 --approve --note "批准，下周生效"
    python scripts/propose.py decide --id PROP-20261116-01 --reject --note "样本不足"
"""
from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "trader" / "memory"
REPORTS = ROOT / "trader" / "reports"
STATUS_LABEL = {"pending": "待批", "approved": "已批准", "rejected": "已驳回",
                "applied": "已落实"}


def _next_id(today: str) -> str:
    day = today.replace("-", "")
    pattern = re.compile(rf"PROP-{day}-(\d{{2}})")
    used = {int(m.group(1)) for path in REPORTS.glob(f"PROP-{day}-*.md") if (m := pattern.search(path.name))}
    return f"PROP-{day}-{max(used, default=0) + 1:02d}"


def _read_status(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^status:\s*(\w+)", text, re.M)
    return match.group(1) if match else "pending"


def cmd_new(args: argparse.Namespace) -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    prop_id = _next_id(today)
    path = REPORTS / f"{prop_id}.md"
    path.write_text(
        f"""---
id: {prop_id}
date: {today}
status: pending
kind: {args.kind}
run: {args.run or ''}
playbook: {args.playbook or ''}
---

# 提案 {prop_id}：{args.title}

## 触发原因

{args.reason or '（周度自评 / 新知识回测发现）'}

## 证据

- 回测：`{args.run or '（待补）'}`
- 关键数字：{args.evidence or '（笔数 / 胜率 / 盈亏比 / 期望 / 回撤）'}

## 建议变更

{args.change or '（改哪条规则、哪个参数、影响哪个打法）'}

## 影响预估

{args.impact or '（信号数、期望、回撤的变化）'}

## 决策

- [ ] 批准（回「批准」后由 Agent 落实并 git 提交）
- [ ] 驳回（写明原因）

## 备注

（人类审批意见写在这里）
""",
        encoding="utf-8",
    )
    print(f"proposal: {path.relative_to(ROOT)}  （状态 pending，等人类批准）")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = sorted(REPORTS.glob("PROP-*.md"))
    if not rows:
        print("没有提案")
        return 0
    for path in rows:
        title = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# 提案"):
                title = line.split("：", 1)[-1]
                break
        status = _read_status(path)
        print(f"{path.stem}  {STATUS_LABEL.get(status, status):<6} {title}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    path = REPORTS / f"{args.id}.md"
    if not path.exists():
        print(f"找不到提案：{path.name}")
        return 2
    status = "approved" if args.approve else "rejected"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^status:\s*\w+", f"status: {status}", text, count=1, flags=re.M)
    text = text.rstrip() + f"\n\n- {date.today().isoformat()} 人类决定：**{STATUS_LABEL[status]}**"
    if args.note:
        text += f"——{args.note}"
    text += "\n"
    path.write_text(text, encoding="utf-8")
    print(f"{args.id} -> {STATUS_LABEL[status]}")
    if status == "approved":
        print("下一步：Agent 按提案改配置/策略，跑回归回测，写 journal 并 git 提交。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Every PROP- citation in memory files must point at a real proposal file."""
    existing = {path.stem for path in REPORTS.glob("PROP-*.md")}
    cited: dict[str, str] = {}
    bad_format: list[str] = []
    sources = [MEMORY / "handover.md", MEMORY / "index.md"]
    sources += sorted((MEMORY / "journal").glob("*.md"))
    sources += sorted((ROOT / "trader" / "plans").glob("*.md"))
    pattern = re.compile(r"PROP-(\d{4})-?(\d{2})-?(\d{2})-(\d{1,2})")
    for path in sources:
        if not path.exists():
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            year, month, day, seq = match.groups()
            canonical = f"PROP-{year}{month}{day}-{int(seq):02d}"
            cited[canonical] = path.name
            if match.group(0) != canonical:
                bad_format.append(f"{path.name} 里的 {match.group(0)} 应写成 {canonical}")
    problems = [f"{name} 引用了不存在的提案 {prop}"
                for prop, name in sorted(cited.items()) if prop not in existing]
    problems += bad_format
    if problems:
        print("提案引用检查失败：")
        for item in problems:
            print("  -", item)
        print("要么用 propose.py new 建卡，要么删掉引用；编号统一写成 PROP-YYYYMMDD-NN。")
        return 1
    print(f"提案检查通过：{len(existing)} 张提案文件，{len(cited)} 处引用全部有效。")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Mark an approved proposal as applied, after checking the approved state.

    The actual edit (config/strategy/risk) stays with the Agent, because each
    kind needs a different regression check. This command owns the state change
    and the audit record so "批准了但忘了落实" cannot happen silently.
    """
    path = REPORTS / f"{args.id}.md"
    if not path.exists():
        print(f"找不到提案：{path.name}")
        return 2
    status = _read_status(path)
    if status == "applied":
        print(f"{args.id} 已经是「{STATUS_LABEL['applied']}」状态")
        return 0
    if status != "approved":
        print(f"{args.id} 当前是「{STATUS_LABEL.get(status, status)}」，只有批准后才能落实")
        return 2
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^status:\s*\w+", "status: applied", text, count=1, flags=re.M)
    text = text.rstrip() + (
        f"\n\n## 落实记录\n\n"
        f"- {date.today().isoformat()} 状态 approved → applied\n"
        f"- 变更内容：{args.change or '（见上文「建议变更」）'}\n"
        f"- 回归回测：{args.regression or '（待补 run id）'}\n"
        f"- 提交：{args.commit or '（待补 commit hash）'}\n"
    )
    path.write_text(text, encoding="utf-8")
    print(f"{args.id} -> {STATUS_LABEL['applied']}")
    print("落实清单：")
    print("  1) 改配置/策略（风险层改 trader/paper/risk.json 或 allocation.json，先备份）")
    print("  2) 跑回归回测，把 run id 填进落实记录")
    print("  3) python scripts/intake.py check && python scripts/propose.py check")
    print("  4) git commit -m \"apply <id>\" 并把 commit hash 填进落实记录")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提案卡")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="新建提案")
    p.add_argument("--title", required=True)
    p.add_argument("--kind", default="tune", choices=["tune", "new", "retire", "risk"])
    p.add_argument("--run", default="")
    p.add_argument("--playbook", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--evidence", default="")
    p.add_argument("--change", default="")
    p.add_argument("--impact", default="")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("list", help="列出提案")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("apply", help="已批准的提案落实后归档（approved -> applied）")
    p.add_argument("--id", required=True)
    p.add_argument("--change", default="")
    p.add_argument("--regression", default="")
    p.add_argument("--commit", default="")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("check", help="检查 journal/handover/index 引用的提案是否存在")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("decide", help="批准或驳回")
    p.add_argument("--id", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_decide)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())