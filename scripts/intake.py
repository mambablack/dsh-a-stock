#!/usr/bin/env python3
"""知识入库：把一段自然语言知识变成可审计的记忆条目和打法卡。

不做"总结"，只做"建档"：
- 原文一字不改地落盘；
- 抽取可测条件（由 Agent 在 `--playbook` 卡里补），状态一律 candidate；
- 自动维护 `trader/memory/index.md` 的两张表。

命令
----
    python scripts/intake.py new --type A --title "标题" --text "用户原话" [--playbook]
    python scripts/intake.py new --type B --title "复盘教训" --file note.md
    python scripts/intake.py list
    python scripts/intake.py index
    python scripts/intake.py check
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "trader" / "memory"
KNOWLEDGE = MEMORY / "knowledge"
PLAYBOOK = MEMORY / "playbook"
INDEX = MEMORY / "index.md"

TYPE_LABEL = {"A": "A 规则", "B": "B 教训", "C": "C 观察", "D": "D 理念"}
STATUS_LABEL = {
    "candidate": "candidate（候选）",
    "tracking": "tracking（纸面跟踪）",
    "verified": "verified（已验证）",
    "retired": "retired（已淘汰）",
}


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------
def _strip_comment(value: str) -> str:
    """Drop a trailing `  # comment` and surrounding quotes from a scalar."""
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0]
    return value.strip().strip('"').strip("'")


def read_frontmatter(path: Path) -> dict[str, str]:
    """Parse the leading `---` YAML block (flat key: value only)."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if match is None:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = _strip_comment(value)
    return data


def _slug(title: str, limit: int = 30) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", title).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:limit] or "untitled"


def _next_id(folder: Path, prefix: str, today: str, width: int) -> str:
    day = today.replace("-", "")
    pattern = re.compile(rf"{prefix}-{day}-(\d{{{width}}})")
    used = {int(m.group(1)) for path in folder.glob(f"{prefix}-{day}-*.md")
            if (m := pattern.search(path.name))}
    return f"{prefix}-{today.replace('-', '')}-{max(used, default=0) + 1:0{width}d}"


def _next_pb_id() -> str:
    pattern = re.compile(r"PB-(\d{3})")
    used = {int(m.group(1)) for path in PLAYBOOK.glob("PB-*.md") if (m := pattern.search(path.name))}
    return f"PB-{max(used, default=0) + 1:03d}"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_new(args: argparse.Namespace) -> int:
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    PLAYBOOK.mkdir(parents=True, exist_ok=True)

    body = args.text
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body:
        body = sys.stdin.read()
    body = body.strip()
    if body == "":
        print("没有内容：用 --text / --file 或 stdin 提供原文")
        return 2

    today = args.date or date.today().isoformat()
    knowledge_id = _next_id(KNOWLEDGE, "K", today, 3)
    path = KNOWLEDGE / f"{knowledge_id}-{_slug(args.title)}.md"
    tags = ", ".join(t.strip() for t in (args.tags or "").split(",") if t.strip())
    testable = "true" if args.testable else "false"
    path.write_text(
        f"""---
id: {knowledge_id}
date: {today}
source: {args.source}
type: {args.type}
tags: [{tags}]
testable: {testable}
---

## 原文

> {body.replace(chr(10), chr(10) + '> ')}

## 结构化

- 适用市场/周期：
- 买入条件：
- 卖出条件：
- 隐含参数：
- 无法量化的部分：

## 我的翻译（可执行版）

- 条件树草案：
- 对应 strategy-lab 特征：

## 检验计划

- 回测策略 id：
- 期望验证什么：

## 检验结果

（待回测）
""",
        encoding="utf-8",
    )
    print(f"knowledge: {path.relative_to(ROOT)}")

    card = None
    if args.playbook:
        pb_id = _next_pb_id()
        card = PLAYBOOK / f"{pb_id}-{_slug(args.title)}.md"
        card.write_text(
            f"""# 打法卡 {pb_id}：{args.title}

```yaml
id: {pb_id}
name: {args.title}
status: candidate
created: {today}
from_knowledge: {knowledge_id}
strategy_file:
backtest_run:
```

## 一句话

（用一句话写清：什么条件下买、什么条件下卖）

## 规则（草案）

| 规则 | 条件 |
|---|---|
| 触发 |  |
| 过滤 |  |
| 卖出 |  |

## 证据

（回测跑完后填：笔数 / 胜率 / 盈亏比 / 每笔期望 / 回撤 / 样本内外）

## 结论

（支持 / 部分支持 / 证伪 / 样本不足）

## 晋级条件

纸面成交 20 笔且期望 > 0 → `verified`；滚动 20 笔期望 < −1% → `retired`。
""",
            encoding="utf-8",
        )
        print(f"playbook : {card.relative_to(ROOT)}")

    regenerate_index()
    print("index    : 已更新 trader/memory/index.md")
    print("\n下一步：1) 补全「结构化」；2) 写成 strategy JSON 并回测；3) 把结果写回条目与卡片。")
    return 0


def _knowledge_rows() -> list[dict[str, str]]:
    rows = []
    for path in sorted(KNOWLEDGE.glob("K-*.md")):
        meta = read_frontmatter(path)
        rows.append({
            "id": meta.get("id", path.stem.split("-")[0]),
            "date": meta.get("date", ""),
            "title": path.stem.split("-", 4)[-1] if "-" in path.stem else path.stem,
            "type": TYPE_LABEL.get(meta.get("type", ""), meta.get("type", "—")),
            "testable": "是" if meta.get("testable") == "true" else "否",
            "status": _knowledge_status(path),
            "file": path.name,
        })
    return rows


def _knowledge_status(path: Path) -> str:
    """已回测 when the 检验结果 section has real content, otherwise 待回测."""
    text = path.read_text(encoding="utf-8")
    if "## 检验结果" not in text:
        return "无检验段"
    tail = text.split("## 检验结果", 1)[1]
    return "待回测" if "待回测" in tail or tail.strip() == "" else "已回测"


def _playbook_rows() -> list[dict[str, str]]:
    rows = []
    for path in sorted(PLAYBOOK.glob("PB-*.md")):
        text = path.read_text(encoding="utf-8")
        block = re.search(r"```yaml\n(.*?)\n```", text, re.S)
        meta: dict[str, str] = {}
        if block:
            for line in block.group(1).splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = _strip_comment(value)
        rows.append({
            "id": meta.get("id", path.stem.split("-")[0]),
            "name": meta.get("name", path.stem),
            "status": STATUS_LABEL.get(meta.get("status", ""), meta.get("status", "—")),
            "knowledge": meta.get("from_knowledge", "—"),
            "file": path.name,
        })
    return rows


def regenerate_index() -> None:
    text = INDEX.read_text(encoding="utf-8")

    pb = _playbook_rows()
    pb_table = ["| ID | 名称 | 状态 | 来源知识 |", "|---|---|---|---|"]
    for row in pb:
        pb_table.append(f"| {row['id']} | {row['name']} | {row['status']} | {row['knowledge']} |")
    if not pb:
        pb_table.append("| — | （还没有打法卡） | — | — |")

    kn = _knowledge_rows()
    kn_table = ["| ID | 日期 | 标题 | 类型 | 可测 | 状态 |", "|---|---|---|---|---|---|"]
    for row in kn:
        kn_table.append(f"| {row['id']} | {row['date']} | {row['title']} | {row['type']} | "
                        f"{row['testable']} | {row['status']} |")
    if not kn:
        kn_table.append("| — | — | （还没有知识条目） | — | — | — |")

    text = re.sub(r"(<!-- AUTO:PLAYBOOK -->).*?(<!-- /AUTO:PLAYBOOK -->)",
                  lambda m: m.group(1) + "\n" + "\n".join(pb_table) + "\n" + m.group(2),
                  text, flags=re.S)
    text = re.sub(r"(<!-- AUTO:KNOWLEDGE -->).*?(<!-- /AUTO:KNOWLEDGE -->)",
                  lambda m: m.group(1) + "\n" + "\n".join(kn_table) + "\n" + m.group(2),
                  text, flags=re.S)
    INDEX.write_text(text, encoding="utf-8")


def cmd_index(args: argparse.Namespace) -> int:
    regenerate_index()
    print(f"index 已重建：{INDEX.relative_to(ROOT)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for row in _playbook_rows():
        print(f"{row['id']}  {row['status']:<22} {row['name']}")
    print()
    for row in _knowledge_rows():
        print(f"{row['id']}  {row['date']}  {row['type']:<6} {row['status']:<6} {row['title']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    problems: list[str] = []
    ids: set[str] = set()
    for path in KNOWLEDGE.glob("K-*.md"):
        meta = read_frontmatter(path)
        kid = meta.get("id")
        if not kid:
            problems.append(f"{path.name}: 缺少 id")
            continue
        if kid in ids:
            problems.append(f"{path.name}: id 重复 {kid}")
        ids.add(kid)
        for field in ("date", "type", "testable"):
            if not meta.get(field):
                problems.append(f"{path.name}: 缺少 {field}")
    pb_ids: set[str] = set()
    for path in PLAYBOOK.glob("PB-*.md"):
        text = path.read_text(encoding="utf-8")
        block = re.search(r"```yaml\n(.*?)\n```", text, re.S)
        if block is None:
            problems.append(f"{path.name}: 缺少 yaml 块")
            continue
        meta = {}
        for line in block.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = _strip_comment(value)
        pid = meta.get("id")
        if not pid:
            problems.append(f"{path.name}: 缺少 id")
            continue
        if pid in pb_ids:
            problems.append(f"{path.name}: id 重复 {pid}")
        pb_ids.add(pid)
        if meta.get("status") not in {"candidate", "tracking", "verified", "retired"}:
            problems.append(f"{path.name}: 非法状态 {meta.get('status')!r}")
    index_text = INDEX.read_text(encoding="utf-8")
    for kid in ids:
        if kid not in index_text:
            problems.append(f"index 缺少知识 {kid}（跑 intake.py index）")
    for pid in pb_ids:
        if pid not in index_text:
            problems.append(f"index 缺少打法 {pid}（跑 intake.py index）")

    if problems:
        print("检查失败：")
        for item in problems:
            print("  -", item)
        return 1
    print(f"检查通过：{len(ids)} 条知识、{len(pb_ids)} 张打法卡，均已入索引。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知识入库与打法卡")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="新增一条知识（可同时建候选打法卡）")
    p.add_argument("--type", choices=list(TYPE_LABEL), default="A")
    p.add_argument("--title", required=True)
    p.add_argument("--source", default="用户飞书")
    p.add_argument("--tags", default="")
    p.add_argument("--text", default="")
    p.add_argument("--file", default="")
    p.add_argument("--date", default=None)
    p.add_argument("--testable", action="store_true", default=True)
    p.add_argument("--no-testable", dest="testable", action="store_false")
    p.add_argument("--playbook", action="store_true", help="同时建一张 candidate 打法卡")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("index", help="重建 index.md 的两张表")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("list", help="列出知识与打法")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="校验一致性")
    p.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())