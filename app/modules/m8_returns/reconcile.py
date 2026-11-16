"""回传核对：计划名单 × 附件 → 四类差异 + 两个互不混淆的评分口径。

口径约定（页面、导出、清单同源）：
- 覆盖口径只回答「计划名单里的工单回传了多少」，分母是名单工单；
- 质量口径只回答「已回传的那部分记录字段好不好」，分母是已回传代表记录 × 10 字段；
- 批次结论 = 质量分 × 覆盖率，把两件事合成一个可排名的数，且三项闭合到 100。

绝不把未回传工单按整行缺失塞进质量分母：那会让低回传批次无论附件多干净都拿极低分，
分数只剩覆盖率的信息；也绝不只看附件：那会让只回传 2 条干净记录的批次拿满分。
"""
from __future__ import annotations

import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from ... import config
from ..m5_outliers import service as outlier_service

ZERO = Decimal("0")
EVIDENCE_LIMIT = 200


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return ZERO
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def _q(value: Decimal) -> Decimal:
    return value.quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)


def build(ingest: dict, profile: dict, roster: dict) -> dict:
    roster_tickets: list[str] = roster["tickets"]
    roster_set = set(roster_tickets)
    rows = profile["rows"]
    columns = [column["name"] for column in profile["columns"]]
    labels = {column["name"]: column["label"] for column in profile["columns"]}
    key_column = config.BUSINESS_KEYS[0]

    grouped: dict[str, list[dict]] = {}
    off_roster: list[dict] = []
    for row in rows:
        ticket = _ticket_of(row, key_column)
        if ticket and ticket in roster_set:
            grouped.setdefault(ticket, []).append(row)
        else:
            off_roster.append(_record_evidence(row, ticket, columns, labels))

    representatives: list[dict] = []
    duplicate_records: list[dict] = []
    for ticket in roster_tickets:
        entries = grouped.get(ticket, [])
        if not entries:
            continue
        entries = sorted(entries, key=lambda item: item["lineno"])
        representatives.append(entries[0])
        for extra in entries[1:]:
            duplicate_records.append(_record_evidence(
                extra, ticket, columns, labels))

    unreturned = [ticket for ticket in roster_tickets if ticket not in grouped]
    missing_fields = []
    clean_records = 0
    for row in representatives:
        gaps = [name for name in columns if row["cells"][name]["state"] == "missing"]
        if gaps:
            missing_fields.append({
                "ticket": _ticket_of(row, key_column),
                "lineno": row["lineno"],
                "fields": gaps,
                "labels": [labels[name] for name in gaps],
            })
        else:
            clean_records += 1

    matched = len(representatives)
    coverage_value = (ZERO if roster["unique"] <= 0
                      else Decimal(matched) / Decimal(roster["unique"]))
    coverage = coverage_value.quantize(config.RATE_QUANT, rounding=ROUND_HALF_UP)
    quality = _quality(profile, representatives, columns)

    quality_score = Decimal(quality["score"]) if quality["defined"] else ZERO
    batch_score = _q(quality_score * coverage) if quality["defined"] else ZERO
    quality_deduction = (_q(config.HUNDRED - quality_score)
                         if quality["defined"] else ZERO)
    coverage_deduction = _q(config.HUNDRED - batch_score - quality_deduction)

    diffs = [
        _diff("unreturned", config.RETURN_DIFF_MAP["unreturned"], len(unreturned),
              unreturned, "工单号"),
        _diff("off_roster", config.RETURN_DIFF_MAP["off_roster"], len(off_roster),
              off_roster, "lineno"),
        _diff("duplicate", config.RETURN_DIFF_MAP["duplicate"],
              len(duplicate_records), duplicate_records, "lineno"),
        _diff("missing_fields", config.RETURN_DIFF_MAP["missing_fields"],
              len(missing_fields), missing_fields, "lineno"),
    ]

    attachment_records = len(rows)
    partition = len(off_roster) + len(duplicate_records) + matched
    flags = []
    if roster["duplicate_count"]:
        flags.append({
            "code": "K_ROSTER_DUP",
            "label": "名单内工单号重复",
            "count": roster["duplicate_count"],
            "detail": "名单内重复工单号只算一个计划工单，不重复计入覆盖率分母",
        })
    for item in (("unreturned", len(unreturned)), ("off_roster", len(off_roster)),
                 ("duplicate", len(duplicate_records)),
                 ("missing_fields", len(missing_fields))):
        if item[1]:
            definition = config.RETURN_DIFF_MAP[item[0]]
            flags.append({"code": definition["code"], "label": definition["label"],
                          "count": item[1], "detail": definition["definition"]})
    return {
        "roster": {
            "unique": roster["unique"],
            "rows": roster["rows"],
            "format": roster["format"],
            "duplicate_tickets": roster["duplicates"][:EVIDENCE_LIMIT],
            "duplicate_count": roster["duplicate_count"],
        },
        "attachment": {
            "accepted": len(rows),
            "quarantine": ingest["counts"]["quarantine"],
            "blank": ingest["counts"]["blank"],
            "raw_records": ingest["counts"]["raw_records"],
            "segments": ingest["segments"],
            "header": ingest["header"],
        },
        "matching": {
            "roster_tickets": roster["unique"],
            "returned_tickets": matched,
            "attachment_records": attachment_records,
            "representative_records": matched,
            "clean_records": clean_records,
            "off_roster_records": len(off_roster),
            "duplicate_records": len(duplicate_records),
            "partition_closed": partition == attachment_records,
            "partition_rule": "附件采纳记录 = 代表记录 + 重复记录 + 名单外记录",
        },
        "diffs": diffs,
        "diff_map": {item["key"]: item["count"] for item in diffs},
        "flags": flags,
        "unreturned": {
            "count": len(unreturned),
            "tickets": unreturned[:EVIDENCE_LIMIT],
            "truncated": len(unreturned) > EVIDENCE_LIMIT,
        },
        "coverage": {
            "rate": str(coverage),
            "numerator": matched,
            "denominator": roster["unique"],
            "missing": len(unreturned),
            "grade": _coverage_grade(coverage),
        },
        "quality": quality,
        "batch": {
            "score": str(batch_score),
            "grade": _grade_of(batch_score),
            "quality_deduction": str(quality_deduction),
            "coverage_deduction": str(coverage_deduction),
            "closed": (batch_score + quality_deduction
                       + coverage_deduction == config.HUNDRED),
            "formula": config.RETURN_BATCH_FORMULA,
            "definition": "批次分 = 已回传字段质量分 × 回传覆盖率",
        },
        "formula": {
            "quality": config.RETURN_QUALITY_FORMULA,
            "batch": config.RETURN_BATCH_FORMULA,
            "coverage": "回传覆盖率 = 已回传名单工单数 ÷ 计划名单工单数",
        },
        "scopes": [dict(item) for item in config.RETURN_SCOPES],
        "diff_definitions": [dict(item) for item in config.RETURN_DIFFS],
    }


def _ticket_of(row: dict, column: str) -> str:
    cell = row["cells"].get(column, {})
    if cell.get("state") == "missing":
        return ""
    raw = cell.get("raw") or ""
    return unicodedata.normalize("NFKC", str(raw)).strip()


def _record_evidence(row: dict, ticket: str, columns: list[str],
                     labels: dict[str, str]) -> dict:
    gaps = [name for name in columns if row["cells"][name]["state"] == "missing"]
    return {
        "ticket": ticket or "（无工单号）",
        "lineno": row["lineno"],
        "missing_fields": gaps,
        "missing_labels": [labels[name] for name in gaps],
    }


def _diff(key: str, definition: dict, count: int, items: list,
          unit_field: str) -> dict:
    return {
        "key": key,
        "label": definition["label"],
        "definition": definition["definition"],
        "unit": definition["unit"],
        "code": definition["code"],
        "count": count,
        "items": items[:EVIDENCE_LIMIT],
        "truncated": count > EVIDENCE_LIMIT,
        "unit_field": unit_field,
    }


def _quality(profile: dict, representatives: list[dict],
             columns: list[str]) -> dict:
    """只对已回传代表记录评字段质量；未回传工单完全不进分母。"""
    total = len(representatives) * len(columns)
    if total <= 0:
        return {
            "defined": False,
            "score": None,
            "grade": None,
            "weighted_penalty": None,
            "penalty_sum": None,
            "counts": {"cells_total": 0, "cells_missing": 0, "cells_value": 0,
                       "format_bad": 0, "outlier_cells": 0, "tested_cells": 0},
            "dims": [],
            "formula": config.RETURN_QUALITY_FORMULA,
            "note": "没有任何已回传记录，字段质量分不可计算",
        }
    missing = sum(1 for row in representatives for name in columns
                  if row["cells"][name]["state"] == "missing")
    format_bad = sum(1 for row in representatives for name in columns
                     if row["cells"][name]["state"] == "format_bad")
    subset = {"columns": profile["columns"], "rows": representatives}
    outlier = outlier_service.build(subset)
    tested = outlier["counts"]["tested_cells"]
    outlier_cells = outlier["counts"]["outlier_cells"]

    denominators = {
        "missing": total,
        "format": total - missing,
        "outlier": tested,
    }
    numerators = {
        "missing": missing,
        "format": format_bad,
        "outlier": outlier_cells,
    }
    dims = []
    weighted = ZERO
    for item in config.RETURN_QUALITY_WEIGHTS:
        key = item["key"]
        cap = Decimal(item["cap"])
        tolerance = Decimal(item["tolerance"])
        value = _rate(numerators[key], denominators[key])
        ratio = (value / tolerance) if tolerance > 0 else ZERO
        capped = ratio > 1
        ratio = min(ratio, Decimal("1"))
        penalty = _q(cap * ratio)
        weighted += penalty
        dims.append({
            "key": key, "label": item["label"], "cap": str(cap),
            "tolerance": str(tolerance),
            "numerator": numerators[key],
            "denominator": denominators[key],
            "rate": str(value),
            "ratio": str(ratio.quantize(Decimal("0.0001"),
                                        rounding=ROUND_HALF_UP)),
            "penalty": str(penalty), "capped": capped,
        })
    weighted = _q(weighted)
    penalty_sum = _q(weighted * config.HUNDRED / config.RETURN_QUALITY_CAP_TOTAL)
    score = _q(config.HUNDRED - penalty_sum)
    score = min(max(score, ZERO), config.HUNDRED)
    return {
        "defined": True,
        "score": str(score),
        "grade": _grade_of(score),
        "weighted_penalty": str(weighted),
        "penalty_sum": str(penalty_sum),
        "counts": {
            "cells_total": total,
            "cells_missing": missing,
            "cells_value": total - missing,
            "format_bad": format_bad,
            "outlier_cells": outlier_cells,
            "tested_cells": tested,
        },
        "dims": dims,
        "formula": config.RETURN_QUALITY_FORMULA,
        "note": "只统计已回传代表记录；未回传工单、名单外与重复记录不进分母",
    }


def _grade_of(score: Decimal) -> dict:
    for item in config.GRADES:
        if score >= Decimal(item["floor"]):
            return {"grade": item["grade"], "label": item["label"],
                    "floor": item["floor"]}
    last = config.GRADES[-1]
    return {"grade": last["grade"], "label": last["label"], "floor": last["floor"]}


def _coverage_grade(value: Decimal) -> dict:
    for item in config.COVERAGE_BANDS:
        if value >= Decimal(item["floor"]):
            return {"grade": item["grade"], "label": item["label"],
                    "floor": item["floor"]}
    last = config.COVERAGE_BANDS[-1]
    return {"grade": last["grade"], "label": last["label"], "floor": last["floor"]}


def conclusion(result: dict) -> dict:
    """批次结论：一句话同时给出覆盖与质量，并明确各自能外推到哪。"""
    coverage = result["coverage"]
    quality = result["quality"]
    batch = result["batch"]
    quality_part = (
        f"已回传字段质量 {quality['score']} 分"
        f"（{quality['grade']['grade']}）"
        if quality["defined"] else "已回传字段质量分不可计算")
    coverage_part = (f"覆盖 {coverage['numerator']}/{coverage['denominator']}"
                     f" = {_pct(coverage['rate'])}")
    scope = "字段质量分只代表已回传记录，不能外推为未回传工单的质量；覆盖缺口只在批次分里扣。"
    if coverage["numerator"] == 0:
        scope = "没有任何已回传记录，本批次无法给出字段质量结论。"
    elif Decimal(coverage["rate"]) < Decimal("1"):
        scope += "本批存在未回传工单，整批数据完整性仍需补件后才能定论。"
    return {
        "batch_score": batch["score"],
        "batch_grade": batch["grade"],
        "headline": (f"批次分 {batch['score']} 分 · {batch['grade']['grade']} "
                     f"{batch['grade']['label']}"),
        "detail": f"{coverage_part}；{quality_part}",
        "scope": scope,
        "quality_deduction": batch["quality_deduction"],
        "coverage_deduction": batch["coverage_deduction"],
        "closed": batch["closed"],
    }


def _pct(value) -> str:
    return f"{Decimal(str(value)) * 100:.2f}%"
