"""M8 回传核对：四类差异、两个评分口径、版本替换与重试账。

重点回归：
- 未回传工单不进质量分母（低回传率不能靠整行缺失把质量分拖垮，也不能只看附件虚高）；
- 四类差异互斥且覆盖全部附件采纳记录；
- 替换失败保留完整旧版；同输入重试不重复计数、不丢记录。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config
from app.errors import Conflict, QualiscoreError, ReplaceFailed
from app.modules.m8_returns import reconcile, roster, service
from fixtures import dirty_cases

HEADER = ("ticket_id,customer_id,channel,created_at,first_response_min,"
          "handle_hours,satisfaction,agent,region,priority")


def roster_text(tickets: list[str]) -> str:
    return "ticket_id\n" + "\n".join(tickets) + "\n"


def attachment(rows: list[str]) -> bytes:
    return (HEADER + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def ticket(n: int) -> str:
    return f"T-2026-{n:06d}"


def record(n: int, **over) -> str:
    return dirty_cases.make_row(ticket=ticket(n), customer=f"00{500 + n % 400:03d}",
                               **{key: value for key, value in over.items()})


def case(settings, store, *, batch: str = "b1", tickets: list[int],
         rows: list[str], attachments: bytes | None = None,
         expected_version=None, use_sample: bool = False) -> dict:
    return service.run(
        settings, store, batch=batch, roster_bytes=roster_text(
            [ticket(n) for n in tickets]),
        attachment=attachments if attachments is not None else attachment(rows),
        expected_version=expected_version, use_sample=use_sample)


# ---------- 名单解析 ----------

def test_roster_accepts_plain_lines_and_dedupes():
    parsed = roster.parse("T-2026-000001\nT-2026-000001\nT-2026-000002\n")
    assert parsed["unique"] == 2
    assert parsed["rows"] == 3
    assert parsed["duplicate_count"] == 1
    assert parsed["format"] == "plain"


def test_roster_rejects_empty():
    with pytest.raises(QualiscoreError) as info:
        roster.parse("   \n\n")
    assert info.value.code == "E_ROSTER_EMPTY"


# ---------- 四类差异 ----------

def test_four_diff_classes_and_partition(settings, store):
    tickets = list(range(1, 11))
    rows = [record(n) for n in range(1, 7)]          # 1..6 回传
    rows.append(record(3))                           # 重复
    rows.append(record(200))                         # 名单外
    rows.append(record(201))                         # 名单外
    result = case(settings, store, tickets=tickets, rows=rows)
    diff = {item["key"]: item for item in result["diffs"]}
    assert [item["key"] for item in result["diffs"]] == [
        "unreturned", "off_roster", "duplicate", "missing_fields"]
    assert diff["unreturned"]["count"] == 4          # 7..10
    assert diff["unreturned"]["unit"] == "工单"
    assert diff["off_roster"]["count"] == 2
    assert diff["duplicate"]["count"] == 1
    assert diff["missing_fields"]["count"] == 0
    assert diff["off_roster"]["unit"] == "记录"
    matching = result["matching"]
    assert matching["attachment_records"] == 9
    assert matching["partition_closed"] is True
    assert (matching["representative_records"] + matching["duplicate_records"]
            + matching["off_roster_records"] == matching["attachment_records"])


def test_missing_fields_is_not_unreturned(settings, store):
    tickets = [1, 2, 3]
    rows = [record(1), record(2, agent=""), record(3)]
    result = case(settings, store, tickets=tickets, rows=rows)
    diff = {item["key"]: item for item in result["diffs"]}
    assert diff["missing_fields"]["count"] == 1
    assert diff["unreturned"]["count"] == 0
    item = diff["missing_fields"]["items"][0]
    assert item["ticket"] == ticket(2)
    assert item["fields"] == ["agent"]


# ---------- 两个评分口径 ----------

def test_unreturned_not_in_quality_denominator(settings, store):
    """把 48 单里 40 单回传，未回传 8 单不能被当成整行缺失。"""
    tickets = list(range(1, 49))
    rows = [record(n) for n in range(1, 41)]
    result = case(settings, store, tickets=tickets, rows=rows)
    quality = result["quality"]
    assert quality["counts"]["cells_total"] == 40 * 10
    assert quality["counts"]["cells_missing"] == 0
    assert quality["score"] == "100.00"
    assert result["coverage"]["rate"] == "0.8333"
    assert result["batch"]["score"] == "83.33"
    assert result["batch"]["closed"] is True


def test_low_coverage_cannot_claim_high_batch_score(settings, store):
    """只看附件会得到 100；批次结论必须被覆盖折算压下来。"""
    tickets = list(range(1, 49))
    rows = [record(1), record(2)]
    result = case(settings, store, tickets=tickets, rows=rows)
    assert result["quality"]["score"] == "100.00"
    assert Decimal(result["coverage"]["rate"]) < Decimal("0.05")
    assert Decimal(result["batch"]["score"]) < Decimal("5")
    assert result["coverage"]["grade"]["grade"] == "D"


def test_quality_dims_are_field_only(settings, store):
    tickets = [1, 2, 3, 4]
    rows = [record(1), record(2, satisfaction="9"), record(3, agent=""),
            record(4)]
    result = case(settings, store, tickets=tickets, rows=rows)
    dims = {item["key"]: item for item in result["quality"]["dims"]}
    assert set(dims) == {"missing", "format", "outlier"}
    assert dims["missing"]["numerator"] == 1
    assert dims["missing"]["denominator"] == 40
    assert dims["format"]["numerator"] == 1
    assert Decimal(quality_score(result)) < Decimal("100")


def quality_score(result: dict) -> str:
    return result["quality"]["score"]


def test_batch_closure_and_grade(settings, store):
    tickets = [1, 2, 3]
    rows = [record(1), record(2, agent=""), record(3)]
    result = case(settings, store, tickets=tickets, rows=rows)
    batch = result["batch"]
    total = (Decimal(batch["score"]) + Decimal(batch["quality_deduction"])
             + Decimal(batch["coverage_deduction"]))
    assert total == Decimal("100")
    assert batch["closed"] is True
    assert Decimal("0") <= Decimal(batch["score"]) <= Decimal("100")


def test_no_returned_records_has_no_quality_score(settings, store):
    result = case(settings, store, tickets=[1, 2], rows=[record(900)])
    assert result["quality"]["defined"] is False
    assert result["quality"]["score"] is None
    assert result["coverage"]["rate"] == "0.0000"
    assert result["batch"]["closed"] is True
    assert "无法给出字段质量结论" in result["conclusion"]["scope"]


def test_conclusion_states_scope(settings, store):
    rows = [record(1), record(3)]
    result = case(settings, store, tickets=[1, 2, 3], rows=rows)
    conclusion = result["conclusion"]
    assert "覆盖 2/3" in conclusion["detail"]
    assert "不能外推" in conclusion["scope"]
    assert "补件" in conclusion["scope"]


# ---------- 版本、幂等与替换 ----------

def test_same_input_replay_does_not_double_count(settings, store):
    tickets = [1, 2, 3]
    rows = [record(1), record(2), record(500)]
    first = case(settings, store, tickets=tickets, rows=rows)
    second = case(settings, store, tickets=tickets, rows=rows)
    assert first["replay"] is False
    assert second["replay"] is True
    assert second["run"]["version"] == 1
    assert store.max_return_version("b1") == 1
    assert second["attempt"]["outcome"] == "replay"
    assert second["attempt"]["hits"] == 1
    assert second["diffs"] == first["diffs"]
    status = service.status(store, "b1")
    assert len(status["versions"]) == 1
    outcomes = {item["outcome"]: item["hits"] for item in status["attempts"]}
    assert outcomes["created"] == 1
    assert outcomes["replay"] == 1


def test_new_attachment_creates_next_version(settings, store):
    tickets = [1, 2]
    first = case(settings, store, tickets=tickets, rows=[record(1)])
    second = case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    assert first["run"]["version"] == 1
    assert second["run"]["version"] == 2
    assert second["run"]["previous_version"] == 1
    status = service.status(store, "b1")
    assert status["active_version"] == 2
    assert len(status["versions"]) == 2


def test_replacement_failure_keeps_old_version(settings, store, monkeypatch):
    tickets = [1, 2, 3]
    good = case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    before = store.load_return_result("b1", 1)
    inputs = store.load_return_inputs("b1", 1)
    calls = {"n": 0}

    def boom(self, batch, version, stamp):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("磁盘写入失败")
        original(self, batch, version, stamp)

    original = type(store).activate_return_version
    monkeypatch.setattr(type(store), "activate_return_version", boom)

    with pytest.raises(ReplaceFailed) as info:
        case(settings, store, tickets=tickets, rows=[record(1), record(2), record(3)])
    assert info.value.code == "E_RETURN_REPLACE_FAILED"
    assert info.value.status_code == 500
    assert info.value.detail["preserved_version"] == 1

    monkeypatch.setattr(type(store), "activate_return_version", original)
    active = store.active_return_record("b1")
    assert int(active["version"]) == 1
    assert store.load_return_result("b1", 1) == before
    assert store.load_return_inputs("b1", 1) == inputs
    assert store.get_return_version("b1", 2) is None
    status = service.status(store, "b1")
    assert status["active_version"] == 1
    assert len(status["versions"]) == 1
    failed = [item for item in status["attempts"]
              if item["outcome"] == "replace_failed"]
    assert len(failed) == 1
    assert failed[0]["hits"] == 1
    assert good["run"]["version"] == 1


def test_retry_after_failure_succeeds_without_duplicate_counts(
        settings, store, monkeypatch):
    tickets = [1, 2, 3]
    case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    original = type(store).activate_return_version
    calls = {"n": 0}

    def flip(self, batch, version, stamp):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("切换失败")
        original(self, batch, version, stamp)

    monkeypatch.setattr(type(store), "activate_return_version", flip)
    with pytest.raises(QualiscoreError):
        case(settings, store, tickets=tickets, rows=[record(1), record(2), record(3)])
    monkeypatch.setattr(type(store), "activate_return_version", original)

    retried = case(settings, store, tickets=tickets,
                   rows=[record(1), record(2), record(3)])
    assert retried["replay"] is False
    assert retried["run"]["version"] == 2
    assert store.max_return_version("b1") == 2
    status = service.status(store, "b1")
    outcomes = {}
    for item in status["attempts"]:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + item["hits"]
    assert outcomes["replace_failed"] == 1
    assert outcomes["created"] == 2
    assert len(status["versions"]) == 2
    active = store.active_return_record("b1")
    assert int(active["version"]) == 2
    assert retried["matching"]["returned_tickets"] == 3


def test_interrupted_switch_recovers_snapshot_without_losing_version(
        settings, store, monkeypatch):
    """快照已写、指针未切换就中断：同输入重试要补齐切换并复用，不丢这条记录。"""
    tickets = [1, 2, 3]
    case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    original = type(store).activate_return_version
    calls = {"n": 0}

    def die_before_switch(self, batch, version, stamp):
        calls["n"] += 1
        raise RuntimeError("进程在切换前中断")

    # 让失败路径的删除与回滚也不生效，模拟进程直接退出、快照留在库里。
    monkeypatch.setattr(type(store), "activate_return_version", die_before_switch)
    monkeypatch.setattr(type(store), "delete_return_version",
                        lambda self, batch, version: None)
    monkeypatch.setattr(type(store), "rollback_return_version",
                        lambda self, batch, attempted, fallback, stamp: None)
    with pytest.raises(ReplaceFailed):
        case(settings, store, tickets=tickets, rows=[record(1), record(2), record(3)])
    assert store.get_return_version("b1", 2) is not None
    assert service.status(store, "b1")["active_version"] == 1

    monkeypatch.setattr(type(store), "activate_return_version", original)
    recovered = case(settings, store, tickets=tickets,
                     rows=[record(1), record(2), record(3)])
    assert recovered["replay"] is True
    assert recovered["run"]["version"] == 2
    assert store.max_return_version("b1") == 2
    assert service.status(store, "b1")["active_version"] == 2
    assert recovered["matching"]["returned_tickets"] == 3


def test_stale_version_rejected_and_logged(settings, store):
    tickets = [1, 2]
    case(settings, store, tickets=tickets, rows=[record(1)])
    case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    with pytest.raises(Conflict) as info:
        case(settings, store, tickets=tickets, rows=[record(1)],
             expected_version=1)
    assert info.value.code == "E_STALE_RUN"
    assert info.value.detail["latest_version"] == 2
    assert service.status(store, "b1")["active_version"] == 2


def test_modify_only_attachment_reuses_active_roster(settings, store):
    tickets = [1, 2, 3]
    case(settings, store, tickets=tickets, rows=[record(1), record(2)])
    second = service.run(settings, store, batch="b1",
                         attachment=attachment([record(1), record(2), record(3)]))
    assert second["roster"]["unique"] == 3
    assert second["coverage"]["numerator"] == 3
    assert second["run"]["version"] == 2


# ---------- 页面与导出 ----------

def test_page_shows_both_scopes_and_four_diffs(client):
    response = client.post("/api/returns/check",
                           json={"batch": "demo", "sample": True})
    assert response.status_code == 200, response.text
    page = client.get("/returns?batch=demo")
    assert page.status_code == 200
    html = page.text
    for label in ("回传覆盖率", "已回传字段质量分", "批次分",
                  "未回传", "名单外", "重复记录", "缺字段", "评分口径"):
        assert label in html, label
    assert "0–100" in html or "0%–100%" in html
    assert "不能外推" in html


def test_exports_state_scopes_and_ranges(client):
    client.post("/api/returns/check", json={"batch": "demo", "sample": True})
    report = client.get("/api/export/returns-report.csv?batch=demo")
    assert report.status_code == 200
    text = report.content.decode("utf-8")
    assert text.startswith("﻿")
    for label in ("未回传", "名单外", "重复记录", "缺字段"):
        assert label in text
    assert "范围 0%–100%" in text
    assert "范围 0–100" in text

    diffs = client.get("/api/export/returns-diffs.csv?batch=demo")
    assert diffs.status_code == 200
    diff_text = diffs.content.decode("utf-8")
    assert "未回传" in diff_text and "名单外" in diff_text

    manifest = client.get("/api/export/returns-manifest.json?batch=demo")
    assert manifest.status_code == 200
    payload = manifest.json()
    for key in ("coverage", "quality", "batch"):
        assert key in payload["score_scopes"]
    assert payload["score_scopes"]["quality"]["range"] == "0–100"
    assert payload["score_scopes"]["coverage"]["range"] == "0–1"
    assert len(payload["diffs"]) == 4
    assert payload["matching"]["partition_closed"] is True


def test_sample_batch_matches_doc_numbers(client):
    body = client.post("/api/returns/check",
                       json={"batch": "demo", "sample": True}).json()
    assert body["roster"]["unique"] == 48
    assert body["coverage"]["numerator"] == 40
    assert body["coverage"]["rate"] == "0.8333"
    assert body["quality"]["score"] == "87.85"
    assert body["quality"]["counts"]["cells_total"] == 400
    assert body["diff_map"] == {"unreturned": 8, "off_roster": 2,
                                "duplicate": 2, "missing_fields": 6}
    assert body["batch"]["score"] == "73.21"
    assert body["batch"]["quality_deduction"] == "12.15"
    assert body["batch"]["coverage_deduction"] == "14.64"
    assert (Decimal(body["batch"]["score"])
            + Decimal(body["batch"]["quality_deduction"])
            + Decimal(body["batch"]["coverage_deduction"])) == Decimal("100")


def test_returns_page_empty_state(client):
    page = client.get("/returns")
    assert page.status_code == 200
    assert "还没有回传批次" in page.text


def test_upload_endpoint_with_files(client):
    roster_bytes = roster_text([ticket(1), ticket(2)]).encode("utf-8")
    attach_bytes = attachment([record(1)])
    response = client.post(
        "/api/returns/upload",
        data={"batch": "up", "expected_version": ""},
        files={"roster": ("r.csv", roster_bytes, "text/csv"),
               "attachment": ("a.csv", attach_bytes, "text/csv")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["batch"] == "up"
    assert body["diff_map"]["unreturned"] == 1
    status = client.get("/api/returns/status?batch=up").json()
    assert status["active_version"] == 1
