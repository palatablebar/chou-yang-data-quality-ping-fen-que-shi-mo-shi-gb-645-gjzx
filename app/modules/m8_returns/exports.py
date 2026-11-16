"""回传导出：四类差异明细、结论汇总、运行清单。

每个导出首段都写清口径与取值范围，离线打开 CSV 也能看出「覆盖」与
「已回传字段质量」是两个数、批次分由两者合成。
"""
from __future__ import annotations

import json
from decimal import Decimal

from fastapi.responses import Response

from ..m7_export import csv_out


def _pct(value) -> str:
    return f"{Decimal(str(value)) * 100:.2f}%"


def _meta_rows(result: dict) -> list[list]:
    run = result["run"]
    coverage = result["coverage"]
    quality = result["quality"]
    batch = result["batch"]
    conclusion = result["conclusion"]
    quality_text = quality["score"] if quality["defined"] else "不可计算"
    return [
        ["批次", run["batch"], "", ""],
        ["版本", f"v{run['version']}", "", f"上一版 v{run['previous_version']}"
         if run.get("previous_version") else "首版"],
        ["输入摘要", run["digest"][:16], "", "名单 + 附件 + 规则阈值"],
        ["回传覆盖率", _pct(coverage["rate"]), "%",
         f"{coverage['numerator']}/{coverage['denominator']}，范围 0%–100%"],
        ["已回传字段质量分", quality_text, "分",
         f"范围 0–100，只评已回传 {result['matching']['representative_records']} 条代表记录"],
        ["批次分", batch["score"], "分",
         f"范围 0–100，= {quality_text} × {_pct(coverage['rate'])}"],
        ["等级", batch["grade"]["grade"], "", batch["grade"]["label"]],
        ["质量扣分", batch["quality_deduction"], "分", "100 − 已回传字段质量分"],
        ["覆盖扣分", batch["coverage_deduction"], "分", "质量分 × (1 − 覆盖率)"],
        ["分数闭合", "是" if batch["closed"] else "否", "",
         "批次分 + 质量扣分 + 覆盖扣分 = 100"],
        ["结论", conclusion["headline"], "", conclusion["detail"]],
        ["口径说明", conclusion["scope"], "", ""],
    ]


def report_csv(result: dict) -> str:
    rows = list(_meta_rows(result))
    rows.append(["", "", "", ""])
    rows.append(["差异类", "计数", "单位", "定义"])
    for item in result["diffs"]:
        rows.append([item["label"], item["count"], item["unit"],
                     item["definition"]])
    rows.append(["", "", "", ""])
    rows.append(["质量维度", "比率", "容忍度", "封顶"])
    for item in result["quality"]["dims"]:
        rows.append([item["label"], item["rate"], item["tolerance"], item["cap"]])
    return csv_out.csv_text(["项目", "数值", "单位", "说明"], rows)


DIFF_HEADERS = ["差异类", "工单号", "行号", "缺失字段", "定义"]


def diff_rows(result: dict) -> list[list]:
    rows = []
    for item in result["diffs"]:
        if item["key"] == "unreturned":
            for ticket in item["items"]:
                rows.append([item["label"], ticket, "", "", item["definition"]])
            if item["truncated"]:
                rows.append([item["label"], "（其余略）", "", "", ""])
            continue
        for entry in item["items"]:
            labels = entry.get("labels") or entry.get("missing_labels") or []
            rows.append([item["label"], entry.get("ticket", ""),
                         entry.get("lineno", ""), " ".join(labels),
                         item["definition"]])
        if item["truncated"]:
            rows.append([item["label"], "（其余略）", "", "", ""])
    return rows


def diffs_csv(result: dict) -> str:
    return csv_out.csv_text(DIFF_HEADERS, diff_rows(result))


def manifest_json(result: dict) -> str:
    coverage = result["coverage"]
    quality = result["quality"]
    batch = result["batch"]
    payload = {
        "run": result["run"],
        "manifest": result["manifest"],
        "conclusion": result["conclusion"],
        "score_scopes": {
            "coverage": {
                "value": coverage["rate"],
                "unit": "ratio",
                "range": "0–1",
                "numerator": coverage["numerator"],
                "denominator": coverage["denominator"],
                "definition": "已回传名单工单数 ÷ 计划名单工单数",
            },
            "quality": {
                "value": quality["score"],
                "unit": "score",
                "range": "0–100",
                "defined": quality["defined"],
                "numerator": "已回传代表记录的缺失 / 格式 / 离群扣分",
                "denominator": "已回传代表记录 × 10 字段",
                "definition": quality["formula"],
            },
            "batch": {
                "value": batch["score"],
                "unit": "score",
                "range": "0–100",
                "quality_deduction": batch["quality_deduction"],
                "coverage_deduction": batch["coverage_deduction"],
                "closed": batch["closed"],
                "definition": batch["definition"],
            },
        },
        "diffs": [{key: item[key] for key in
                   ("key", "label", "definition", "unit", "code", "count",
                    "truncated")} for item in result["diffs"]],
        "flags": result["flags"],
        "matching": result["matching"],
        "roster": result["roster"],
        "attachment": result["attachment"],
        "quality_dims": quality["dims"],
        "quality_counts": quality["counts"],
        "scopes": result["scopes"],
        "diff_definitions": result["diff_definitions"],
        "thresholds": result["manifest"]["thresholds"],
        "timezone": result["manifest"]["timezone"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


EXPORTS = {
    "returns-report.csv": ("returns-report.csv", "text/csv; charset=utf-8",
                           report_csv),
    "returns-diffs.csv": ("returns-diffs.csv", "text/csv; charset=utf-8",
                          diffs_csv),
    "returns-manifest.json": ("returns-manifest.json", "application/json; charset=utf-8",
                              manifest_json),
}


def response(kind: str, record: dict, result: dict) -> Response:
    filename, media, builder = EXPORTS[kind]
    content = builder(result)
    name = f"{record['batch']}-v{record['version']}-{filename}"
    return Response(content=content.encode("utf-8"), media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
