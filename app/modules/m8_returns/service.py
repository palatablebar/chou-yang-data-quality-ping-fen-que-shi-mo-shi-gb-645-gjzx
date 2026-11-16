"""M8 编排：名单 + 附件 → 核对结果，带版本、幂等与失败回滚。

版本与替换语义：
- 同批次 + 同输入摘要命中已有版本 → 直接复用，不写新版本、不换指针；
- 新输入 → 先写版本快照（含两份输入原文），成功后单独切换 active 指针；
- 切换失败 → 指针回滚到旧版本，旧版输入与结果完整保留，抛 `E_RETURN_REPLACE_FAILED`；
- 每次尝试都记一条尝试账：同输入重试只累加 hits，不新增版本、不丢证据。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from decimal import Decimal

from ... import config, manifest
from ...errors import (Conflict, NotFound, ReplaceFailed, UnprocessableInput)
from ..m1_ingest import decode as m1_decode
from ..m1_ingest import service as m1_service
from ..m2_profile import service as m2_service
from . import reconcile, roster as roster_mod

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(batch: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(batch, threading.Lock())


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def sample_roster(settings: config.Settings) -> str:
    path = _sample_path(settings, "returns_roster_48.csv")
    return path.read_text(encoding="utf-8")


def sample_attachment(settings: config.Settings) -> bytes:
    return _sample_path(settings, "returns_attachment_partial.csv").read_bytes()


def _sample_path(settings: config.Settings, name: str):
    from pathlib import Path
    path = Path(settings.sample_dir) / Path(name).name
    if not path.exists():
        raise NotFound(f"回传样例缺失：{name}", code="E_SAMPLE_UNKNOWN")
    return path


def resolve_inputs(settings: config.Settings, store, *, batch: str,
                   roster: bytes | str | None, attachment: bytes | None,
                   declared: str | None = None,
                   use_sample: bool = False) -> tuple[str, bytes]:
    """确定本次的名单与附件：显式提供 > 样例 > 当前生效版本。"""
    roster_text = _text(roster, declared)
    if use_sample and roster_text is None and attachment is None:
        roster_text = sample_roster(settings)
        attachment = sample_attachment(settings)
    elif roster_text is None or attachment is None:
        active = store.active_return_record(batch)
        inputs = (store.load_return_inputs(batch, int(active["version"]))
                  if active else None)
        if inputs is None:
            raise UnprocessableInput(
                "批次还没有基线版本，需要同时提供名单与附件",
                code="E_RETURN_NO_BASE")
        if roster_text is None:
            roster_text = inputs["roster_text"]
        if attachment is None:
            attachment = inputs["attachment"]
    if not roster_text or not roster_text.strip():
        raise UnprocessableInput("计划名单为空", code="E_ROSTER_EMPTY")
    if not attachment:
        raise UnprocessableInput("附件内容为空", code="E_EMPTY_UPLOAD")
    return roster_text, attachment


def _text(value: bytes | str | None, declared: str | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return m1_decode.decode_bytes(value, declared).text


def digest_of(roster_text: str, attachment: bytes,
              rule_version: str) -> tuple[str, str, str]:
    roster_digest = manifest.digest_bytes(roster_text.encode("utf-8"))
    attachment_digest = manifest.digest_bytes(attachment)
    digest = manifest.digest_parts([
        "returns", roster_digest, attachment_digest,
        manifest.rules_digest(rule_version)])
    return digest, roster_digest, attachment_digest


def run(settings: config.Settings, store, *, batch: str,
        roster_bytes: bytes | str | None = None,
        attachment: bytes | None = None,
        declared: str | None = None,
        expected_version: int | None = None,
        use_sample: bool = False) -> dict:
    batch = (batch or config.DEFAULT_RETURN_BATCH).strip() or config.DEFAULT_RETURN_BATCH
    expected_version = _int_or_none(expected_version)
    roster_text, attachment = resolve_inputs(
        settings, store, batch=batch, roster=roster_bytes,
        attachment=attachment, declared=declared, use_sample=use_sample)
    return _run_locked(settings, store, batch=batch, roster_text=roster_text,
                       attachment=attachment, declared=declared,
                       expected_version=expected_version)


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_locked(settings, store, *, batch: str, roster_text: str,
                attachment: bytes, declared: str | None,
                expected_version: int | None) -> dict:
    with _lock_for(batch):
        current = store.active_return_record(batch)
        current_version = int(current["version"]) if current else 0
        digest, roster_digest, attachment_digest = digest_of(
            roster_text, attachment, settings.rule_version)
        if expected_version is not None and expected_version < current_version:
            _record_attempt(store, batch, digest, "stale", current_version,
                            {"expected_version": expected_version,
                             "active_version": current_version})
            raise Conflict(
                f"提交版本 {expected_version} 落后于当前版本 {current_version}",
                code="E_STALE_RUN",
                detail={"expected_version": expected_version,
                        "latest_version": current_version})

        existing = store.find_return_version(batch, digest)
        if existing is not None:
            # 快照与两份输入同事务写完后才切指针；能找到的完整版本一定可再次生效。
            # 若上次进程在「写好快照、未切指针」之间中断，这里补齐切换，避免重试丢版本。
            active = store.active_return_record(batch)
            if active is None or int(active["version"]) != int(existing["version"]):
                store.activate_return_version(batch, int(existing["version"]), _now())
            store.touch_return_version(batch, int(existing["version"]), _now())
            attempt = _record_attempt(store, batch, digest, "replay",
                                      int(existing["version"]),
                                      {"note": "同输入重试复用同一版本"})
            result = store.load_return_result(batch, int(existing["version"])) or {}
            result = dict(result)
            result["run"] = {**result.get("run", {}), "active": True}
            result["replay"] = True
            result["attempt"] = attempt
            return result

        started = _now()
        clock = time.perf_counter()
        version = store.max_return_version(batch) + 1
        result = _compute(settings, store, batch=batch, version=version,
                          roster_text=roster_text, attachment=attachment,
                          declared=declared, digest=digest,
                          roster_digest=roster_digest,
                          attachment_digest=attachment_digest,
                          started=started, clock=clock)
        try:
            store.save_return_version(
                batch=batch, version=version, digest=digest,
                roster_digest=roster_digest, attachment_digest=attachment_digest,
                roster_text=roster_text, attachment=attachment, result=result,
                stamp=_now())
            store.activate_return_version(batch, version, _now())
        except Exception as exc:  # 写入或切换失败：旧版必须继续生效
            # 未生效的快照删掉；旧版本的原文与结果不动，重试会重新算同一版本号。
            try:
                store.delete_return_version(batch, version)
            except Exception:
                pass
            _record_attempt(store, batch, digest, "replace_failed", version,
                            {"error": str(exc),
                             "active_version": store.active_return_version(batch)})
            try:
                store.rollback_return_version(batch, version, current_version,
                                              _now())
            except Exception:
                pass
            raise ReplaceFailed(
                "附件替换失败，旧版仍生效",
                detail={"batch": batch, "active_version": current_version,
                        "attempted_version": version,
                        "preserved_version": current_version,
                        "reason": str(exc)}) from exc

        attempt = _record_attempt(store, batch, digest, "created", version,
                                  {"note": "新版本已生效"})
        result = dict(result)
        result["replay"] = False
        result["attempt"] = attempt
        return result


def _record_attempt(store, batch: str, digest: str, outcome: str,
                    version: int | None, detail: dict) -> dict:
    return store.record_return_attempt(
        batch=batch, digest=digest, outcome=outcome, version=version,
        detail=detail, stamp=_now())


def _compute(settings, store, *, batch: str, version: int, roster_text: str,
             attachment: bytes, declared: str | None, digest: str,
             roster_digest: str, attachment_digest: str,
             started: str, clock: float) -> dict:
    roster = roster_mod.parse(roster_text)
    ingested = m1_service.ingest(settings, name=f"{batch}-attachment.csv",
                                 data=attachment, declared=declared)
    profile = m2_service.build(ingested)
    result = reconcile.build(ingested, profile, roster)
    concluded = reconcile.conclusion(result)

    finished = _now()
    active = store.active_return_record(batch)
    previous = int(active["version"]) if active else None
    result["run"] = {
        "batch": batch,
        "version": version,
        "previous_version": previous,
        "digest": digest,
        "roster_digest": roster_digest,
        "attachment_digest": attachment_digest,
        "rule_version": settings.rule_version,
        "created_at": started,
        "finished_at": finished,
        "elapsed_ms": int((time.perf_counter() - clock) * 1000),
        "active": True,
    }
    result["attachment"]["name"] = ingested["name"]
    result["attachment"]["encoding"] = ingested["encoding"]
    result["conclusion"] = concluded
    result["manifest"] = {
        "batch": batch,
        "version": version,
        "rule_version": settings.rule_version,
        "input_digest": digest,
        "roster_digest": roster_digest,
        "attachment_digest": attachment_digest,
        "thresholds": {
            "quality": [{"key": item["key"], "cap": item["cap"],
                         "tolerance": item["tolerance"]}
                        for item in config.RETURN_QUALITY_WEIGHTS],
            "coverage_bands": [dict(item) for item in config.COVERAGE_BANDS],
            "grades": [dict(item) for item in config.GRADES],
        },
        "formula": dict(result["formula"]),
        "scopes": [dict(item) for item in config.RETURN_SCOPES],
        "precedence": "缺失 > 格式不合格 > 离群；未回传不进质量分母",
        "timezone": "入库 UTC，展示 Asia/Shanghai(+08:00)",
    }
    return result


def latest(store, batch: str | None = None) -> tuple[dict, dict]:
    """只认生效指针：写过快照但未成功切换的版本不算批次结论。"""
    record = (store.active_return_record(batch) if batch
              else store.active_return_record_any())
    if record is None:
        raise NotFound("还没有回传批次", code="E_RUN_NOT_FOUND")
    result = store.load_return_result(record["batch"], int(record["version"]))
    if result is None:
        raise NotFound("回传结果快照缺失", code="E_RUN_NOT_FOUND")
    return record, result


def status(store, batch: str) -> dict:
    active = store.active_return_record(batch)
    if active is None:
        return {"batch": batch, "active_version": 0, "versions": [],
                "attempts": store.list_return_attempts(batch)}
    result = store.load_return_result(batch, int(active["version"])) or {}
    return {
        "batch": batch,
        "active_version": int(active["version"]),
        "active": {
            "version": int(active["version"]),
            "digest": active["digest"],
            "roster_digest": active["roster_digest"],
            "attachment_digest": active["attachment_digest"],
            "updated_at": active["updated_at"],
        },
        "versions": [_public_version(item) for item in
                     store.list_return_versions(batch)],
        "attempts": store.list_return_attempts(batch),
        "coverage": result.get("coverage"),
        "quality_score": (result.get("quality") or {}).get("score"),
        "batch_score": (result.get("batch") or {}).get("score"),
    }


def _public_version(item: dict) -> dict:
    return {
        "version": int(item["version"]),
        "digest": item["digest"],
        "roster_digest": item["roster_digest"],
        "attachment_digest": item["attachment_digest"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def number(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))
