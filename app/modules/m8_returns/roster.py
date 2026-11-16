"""计划名单解析：一行一个工单号，或带表头的 CSV。

名单只回答「这一批应该回传哪些工单」，不承载任何质量判定。名单内重复的工单号
只算一个计划工单，同时单独计数暴露出来，避免把「名单写重」误算成「多回传」。
"""
from __future__ import annotations

import csv
import io
import unicodedata

from ...errors import UnprocessableInput

ROSTER_ALIASES = ("ticket_id", "工单号", "工单编号", "ticket")


def normalize_ticket(value: str | None) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _pick_column(header: list[str]) -> int | None:
    cleaned = [normalize_ticket(cell).lower() for cell in header]
    for index, cell in enumerate(cleaned):
        if cell in ROSTER_ALIASES:
            return index
    return None


def parse(text: str) -> dict:
    """返回 {tickets, unique, duplicates, rows, header_index, format}。"""
    if not text or not text.strip():
        raise UnprocessableInput("计划名单为空", code="E_ROSTER_EMPTY")

    rows = list(csv.reader(io.StringIO(text)))
    rows = [row for row in rows if any(normalize_ticket(cell) for cell in row)]
    if not rows:
        raise UnprocessableInput("计划名单没有有效行", code="E_ROSTER_EMPTY")

    header_index = _pick_column(rows[0])
    if header_index is None:
        header_index = 0
        data = rows
        form = "plain"
    else:
        data = rows[1:]
        form = "csv"

    tickets: list[str] = []
    duplicates: list[dict] = []
    seen: dict[str, int] = {}
    for row in data:
        if header_index >= len(row):
            continue
        ticket = normalize_ticket(row[header_index])
        if not ticket:
            continue
        if ticket in seen:
            seen[ticket] += 1
            if not any(item["ticket"] == ticket for item in duplicates):
                duplicates.append({"ticket": ticket, "count": seen[ticket]})
            continue
        seen[ticket] = 1
        tickets.append(ticket)

    if not tickets:
        raise UnprocessableInput("计划名单没有工单号", code="E_ROSTER_EMPTY")
    return {
        "tickets": tickets,
        "unique": len(tickets),
        "rows": sum(seen.values()),
        "duplicates": duplicates,
        "duplicate_count": sum(item["count"] - 1 for item in duplicates),
        "format": form,
    }
