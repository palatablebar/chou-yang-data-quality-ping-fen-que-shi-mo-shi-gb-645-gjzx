"""M8 入口：回传核对页、批次结论 API、版本与尝试账、回传导出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ... import config
from ...errors import BadRequest, NotFound, QualiscoreError
from ...web import Ctx, get_ctx, render
from . import exports, service

router = APIRouter(tags=["M8 回传核对"])


def _form_batch(batch: str) -> str:
    return (batch or config.DEFAULT_RETURN_BATCH).strip() or config.DEFAULT_RETURN_BATCH


def _public(result: dict) -> dict:
    return {
        "run": result["run"],
        "conclusion": result["conclusion"],
        "roster": result["roster"],
        "attachment": result["attachment"],
        "matching": result["matching"],
        "diffs": result["diffs"],
        "diff_map": result["diff_map"],
        "flags": result["flags"],
        "unreturned": result["unreturned"],
        "coverage": result["coverage"],
        "quality": result["quality"],
        "batch": result["batch"],
        "formula": result["formula"],
        "scopes": result["scopes"],
        "diff_definitions": result["diff_definitions"],
        "manifest": result["manifest"],
        "attempt": result.get("attempt"),
        "replay": result.get("replay", False),
    }


@router.post("/api/returns/check")
def api_check(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    batch = _form_batch(str(body.get("batch") or ""))
    roster = body.get("roster")
    attachment = body.get("attachment")
    if attachment is not None and not isinstance(attachment, str):
        raise BadRequest("attachment 需要是 CSV 文本")
    result = service.run(
        ctx.settings, ctx.store, batch=batch,
        roster_bytes=roster if roster is not None else None,
        attachment=(attachment.encode("utf-8") if attachment is not None else None),
        declared=body.get("encoding"),
        expected_version=body.get("expected_version"),
        use_sample=bool(body.get("sample")))
    return _public(result)


@router.post("/api/returns/upload")
async def api_upload(batch: str = Form(default=""),
                     roster: UploadFile | None = File(default=None),
                     attachment: UploadFile | None = File(default=None),
                     encoding: str = Form(default=""),
                     expected_version: str = Form(default=""),
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    roster_bytes = await roster.read() if roster is not None else None
    attachment_bytes = await attachment.read() if attachment is not None else None
    result = service.run(
        ctx.settings, ctx.store, batch=_form_batch(batch),
        roster_bytes=roster_bytes, attachment=attachment_bytes,
        declared=encoding or None,
        expected_version=service.number(expected_version))
    return _public(result)


@router.get("/api/returns")
def api_latest(batch: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    record, result = service.latest(ctx.store, batch)
    payload = _public(result)
    payload["run"] = {**payload["run"], "batch": record["batch"],
                      "version": int(record["version"])}
    return payload


@router.get("/api/returns/status")
def api_status(batch: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.status(ctx.store, _form_batch(batch or ""))


@router.get("/api/returns/{batch}/versions/{version}")
def api_version(batch: str, version: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    record = ctx.store.get_return_version(batch, version)
    if record is None:
        raise NotFound(f"回传版本不存在：{batch} v{version}", code="E_RUN_NOT_FOUND")
    result = ctx.store.load_return_result(batch, version) or {}
    return {"version": record["version"], "result": _public(result)}


@router.get("/api/export/{kind}")
def export_returns(kind: str, batch: str | None = None,
                   ctx: Ctx = Depends(get_ctx)):
    if kind not in exports.EXPORTS:
        raise BadRequest(f"未知导出：{kind}")
    record, result = service.latest(ctx.store, batch)
    return exports.response(kind, record, result)


@router.get("/returns")
def page_returns(request: Request, batch: str | None = None,
                 ctx: Ctx = Depends(get_ctx)):
    record, result, error = None, None, None
    try:
        record, result = service.latest(ctx.store, batch)
    except QualiscoreError as exc:  # 空态直接渲染，不把 404 抛给页面
        error = exc.code
    active_batch = record["batch"] if record else (batch or config.DEFAULT_RETURN_BATCH)
    status = service.status(ctx.store, active_batch)
    # 顶部报名条与其余页面同形：批次号 + 当前生效版本 + 规则版本。
    banner = None
    if record is not None:
        banner = {"run_id": record["batch"], "version": int(record["version"])}
    return render(request, "returns.html", page="returns",
                  page_title="回传核对", run=banner,
                  # 空批次时传空 dict，避免 render 回退到别的工单运行记录。
                  result={} if record is None else None,
                  return_record=record, returns=result, return_error=error,
                  return_status=status, return_batch=active_batch,
                  empty_state=(result is None),
                  samples=[{"name": name, "label": label}
                           for name, label in config.RETURN_SAMPLES.items()])
