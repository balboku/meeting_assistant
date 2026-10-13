"""Read-only topic projection shared by the Web reader and document exports.

Never infer links, merge tasks, change review state, call a model or write data.
The persisted structured snapshot includes human confirmations; Markdown parsing
is only a fallback when that snapshot is absent. Unsupported records stay raw.
"""
from __future__ import annotations

from copy import deepcopy
import re

from backend.structured_minutes import parse_standardized_minutes


FIELDS = {"discussions": ("discussion_summary", "D", "topic"),
          "decisions": ("final_decisions", "R", "decision"),
          "actions": ("action_items", "A", "task")}
REVIEW_LABELS = {"generated": "AI 產出，尚未複核", "needs_review": "待重新複核",
                 "reviewed": "已人工複核", "approved": "已核准"}


def _parts(content: str) -> tuple[str, str] | None:
    headings = list(re.finditer(r"^##\s+([^\n]+)", content, re.M))
    terms = ("Discussion Summary|討論摘要", "Final Decisions|最終決議",
             "Action Items|待辦事項", "Verbatim Transcript|完整逐字稿")
    found = [next((h for h in headings if re.search(term, h[1], re.I)), None) for term in terms]
    if any(h is None for h in found) or [h.start() for h in found] != sorted(h.start() for h in found):
        return None
    # Do not silently discard supplementary/custom sections between D/R/A.
    if any(found[0].start() < h.start() < found[3].start() and h not in found for h in headings):
        return None
    # The legacy parser accepts extra prose but cannot account for it. Fail closed
    # rather than hiding a handwritten note or a multiline edit in a topic export.
    known_line = re.compile(
        r"(?:##\s+.*|###\s+D\d+\.\s*.+|-{3,}|-\s*未提及|"
        r"-\s*(?:摘要|背景|重點|影響[/／]風險|待釐清|佐證時間)[：:].*|\|.*\|)", re.I)
    for line in content[found[0].start():found[3].start()].splitlines():
        if line.strip() and not known_line.fullmatch(line.strip()):
            return None
    for start, end, prefix, columns in zip(found, found[1:], ("D", "R", "A"), (0, 5, 7)):
        for line in content[start.end():end.start()].splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip()[1:-1].split("|")]
            if len(cells) != columns:
                return None
            if cells[0].upper() in {"ID", "編號", "#"} or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if not re.fullmatch(prefix + r"\d+", cells[0], re.I):
                return None
    return content[:found[0].start()], content[found[3].start():]


def _seconds(value: str) -> int | None:
    if not re.fullmatch(r"\d{1,3}:[0-5]\d(?::[0-5]\d)?", str(value)):
        return None
    numbers = [int(n) for n in value.split(":")]
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


def _strings(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return [str(value)] if isinstance(value, (str, int, float)) and str(value) else []


def _review_ranges(record: dict) -> list[dict]:
    report = record.get("quality_report") or {}
    if not isinstance(report, dict):
        report = {}
    details = record.get("quality_review_segment_details") or report.get("review_segments") or []
    ranges = []
    for entry in details:
        if not isinstance(entry, dict):
            continue
        try:
            start, end = float(entry["start_seconds"]), float(entry["end_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start <= end:
            ranges.append({"start_seconds": start, "end_seconds": end,
                           "label": str(entry.get("label") or "來源需複核")})
    return ranges


def build_minutes_view(record: dict) -> dict:
    """Produce a display snapshot without mutating the input or database."""
    content = str(record.get("full_content") or "").replace("\r\n", "\n")
    parts = _parts(content)
    fallback = {"available": False, "reason": "請先閱讀原文：此紀錄的格式無法安全整合，未改寫任何內容。"}
    if parts is None:
        return fallback
    snapshot = record.get("structured_summary")
    source = "structured_summary"
    if snapshot is None:
        snapshot = parse_standardized_minutes(content)
        source = "markdown"
        if snapshot["parse_errors"]:
            return fallback
    if not isinstance(snapshot, dict) or snapshot.get("parse_errors"):
        return fallback
    groups, ids = {}, set()
    reviews = {i.get("item_key"): i.get("review_status", "generated")
               for i in record.get("structured_items") or [] if isinstance(i, dict)}
    ranges = _review_ranges(record)
    for target, (field, prefix, required) in FIELDS.items():
        values = snapshot.get(field)
        if not isinstance(values, list):
            return fallback
        groups[target] = []
        for original in values:
            if not isinstance(original, dict):
                return fallback
            item = deepcopy(original)
            key = item.get("id")
            if not isinstance(key, str) or not re.fullmatch(prefix + r"\d+", key) or key in ids:
                return fallback
            if not isinstance(item.get(required), str) or not item[required].strip():
                return fallback
            ids.add(key)
            for name in ("related_discussions", "related_decisions", "key_points", "open_questions",
                         "evidence_timecodes", "source_timecodes", "confirmation_required"):
                item[name] = _strings(item.get(name))
            item["review_status"] = reviews.get(key, "generated")
            codes = item["source_timecodes"] if prefix == "A" else item["evidence_timecodes"]
            item["review_ranges"] = [r for r in ranges if any(
                (seconds := _seconds(code)) is not None and r["start_seconds"] <= seconds <= r["end_seconds"]
                for code in codes)]
            groups[target].append(item)
    if not groups["discussions"]:
        return fallback
    topic_ids = {d["id"] for d in groups["discussions"]}
    # Sibling membership only. An action's related_decisions never implies a topic.
    unassigned = [i["id"] for i in groups["decisions"] + groups["actions"]
                  if not topic_ids.intersection(i["related_discussions"])]
    return {"available": True, "source": source, **groups, "unassigned": unassigned,
            "prefix_markdown": parts[0], "appendix_markdown": parts[1],
            "review_status": record.get("review_status") or "generated"}


def _text(value) -> str:
    return "；".join(_strings(value)) or "未提及"


def _cell(value) -> str:
    return _text(value).replace("|", "｜").replace("\n", "<br>")


def _review_note(item: dict) -> str:
    notes = ["人工審查：" + REVIEW_LABELS.get(item.get("review_status"), "待人工確認")]
    if item.get("review_ranges"):
        notes.append("來源需複核")
    if item.get("confirmation_required"):
        notes.append("欄位待確認")
    if item["id"].startswith("A"):
        for label, field in (("負責人", "owner"), ("期限", "due")):
            if _text(item.get(field)) in {"未提及", "待確認", "不確定"}:
                notes.append(label + "待確認")
        if re.fullmatch(r"(?:發言者|Speaker)\s*[A-Z\d]+", _text(item.get("owner")), re.I):
            notes.append("負責人姓名待確認")
    return "；".join(notes)


def _actions_table(items: list[dict]) -> list[str]:
    if not items:
        return ["原稿未列明此議題的待辦；不代表無需後續行動。", ""]
    lines = ["| # | 任務描述 | 負責人 | 期限 | 優先級 |", "|---|---|---|---|---|"]
    for item in items:
        task = _text(item.get("task")) + "\n關聯議題：" + _text(item["related_discussions"])
        task += "；關聯決議：" + _text(item["related_decisions"])
        task += "\n佐證：" + _text(item["source_timecodes"])
        task += "\n" + _review_note(item)
        lines.append("| " + " | ".join(_cell(v) for v in
                     [item["id"], task, item.get("owner"), item.get("due"), item.get("priority")]) + " |")
    return lines + [""]


def _decision_lines(items: list[dict]) -> list[str]:
    if not items:
        return ["原稿未列明此議題的決議。", ""]
    lines = []
    for item in items:
        status = {"confirmed": "原稿已確認", "pending": "待確認"}.get(item.get("status"), _text(item.get("status")))
        lines += [f"**{item['id']} · {status}**", _text(item.get("decision")),
                  "- 依據／條件：" + _text(item.get("basis")),
                  "- 佐證：" + _text(item["evidence_timecodes"]), "- " + _review_note(item), ""]
    return lines


def topic_minutes_markdown(record: dict, view: dict | None = None) -> str:
    """Export projection; the original Markdown remains byte-for-byte untouched."""
    view = view if view is not None else build_minutes_view(record)
    if not view.get("available"):
        return str(record.get("full_content") or "")
    ds, rs, aa = (view[k] for k in ("discussions", "decisions", "actions"))
    status = REVIEW_LABELS.get(view["review_status"], "待人工確認")
    lines = [view["prefix_markdown"].rstrip(), "", f"> 文件審查狀態：{status}。決議的「原稿已確認」不等於文件核准。",
             "", "## 議題總覽", "", "關聯僅表示議題歸屬，不代表因果或執行順序。", "",
             "| 議題 | 決議 | 待辦 |", "|---|---|---|"]
    for d in ds:
        lines.append("| " + " | ".join([_cell(d["id"] + " " + d["topic"]),
                     _cell([r["id"] for r in rs if d["id"] in r["related_discussions"]]),
                     _cell([a["id"] for a in aa if d["id"] in a["related_discussions"]])]) + " |")
    lines += ["", "## 議題式會議紀錄", ""]
    for d in ds:
        lines += [f"### {d['id']}. {d['topic']}"]
        for label, field in [("討論摘要", "summary"), ("背景", "context"), ("重點", "key_points"),
                             ("影響／風險", "impact"), ("待釐清", "open_questions"), ("佐證時間", "evidence_timecodes")]:
            lines.append(f"- {label}：{_text(d.get(field))}")
        lines.append("- " + _review_note(d))
        lines += ["", "#### 決議", ""] + _decision_lines([r for r in rs if d["id"] in r["related_discussions"]])
        lines += ["#### 後續行動", ""] + _actions_table([a for a in aa if d["id"] in a["related_discussions"]])
    if view["unassigned"]:
        lines += ["## 未明確歸屬議題", "", "保留未歸屬項目，不推測關聯。", ""]
        lines += _decision_lines([r for r in rs if r["id"] in view["unassigned"]])
        lines += _actions_table([a for a in aa if a["id"] in view["unassigned"]])
    lines += ["## 待辦總覽", "", "每個 A 編號列一次；相同工作不自動合併。", ""] + _actions_table(aa)
    lines += [view["appendix_markdown"]]
    return "\n".join(lines)
