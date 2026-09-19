"""GX Portal contract: GET /v1/order-schema, POST /v1/orders, send-report."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from ..datetime_kst import now_kst_date_compact
from ..models import Job, OrderStatus
from ..queue_manager import get_queue_manager
from ..services import get_plugin
from .callbacks import notify_failed, send_report_pdf
from .codes import resolve_daemon_service, to_gx_code
from .fastq_fetch import download_pair, safe_url_for_log
from .mapper import map_daemon_params
from .schemas import SCHEMA_VERSION, get_order_schema, list_schema_codes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["gx-portal"])

_ACTIVE = {
    OrderStatus.DOWNLOADING,
    OrderStatus.QUEUED,
    OrderStatus.RUNNING,
    OrderStatus.PROCESSING,
    OrderStatus.UPLOADING,
    OrderStatus.RECEIVED,
}
_CAN_RESTART = {
    OrderStatus.SAVED,
    OrderStatus.FAILED,
    OrderStatus.CANCELLED,
}


class GxOrganization(BaseModel):
    type: str
    name: Optional[str] = None
    hospital_name: Optional[str] = None
    doctor: Optional[str] = None


class GxSample(BaseModel):
    sample_id: Optional[str] = None
    medical_record_id: Optional[str] = None
    sample_collected_at: Optional[str] = None
    fastq_r1_url: str
    fastq_r2_url: str


class GxCallback(BaseModel):
    report_url: str
    status_url: Optional[str] = None


class GxCreateOrder(BaseModel):
    source: str
    order_id: str
    service_code: str
    schema_version: Optional[str] = None
    organization: GxOrganization
    sample: GxSample
    service_data: Dict[str, Any] = Field(default_factory=dict)
    callback: GxCallback


def _gx_meta(body: GxCreateOrder) -> Dict[str, Any]:
    return {
        "source": "gx-portal",
        "gx_service_code": body.service_code,
        "schema_version": body.schema_version or SCHEMA_VERSION,
        "report_url": body.callback.report_url,
        "status_url": body.callback.status_url,
        "organization": body.organization.model_dump(),
        "sample": {
            "sample_id": body.sample.sample_id,
            "medical_record_id": body.sample.medical_record_id,
            "sample_collected_at": body.sample.sample_collected_at,
        },
        "service_data": body.service_data,
        "fastq_downloaded": False,
    }


def _job_gx(job: Optional[Job]) -> Dict[str, Any]:
    if not job:
        return {}
    meta = (job.params or {}).get("_gx")
    return meta if isinstance(meta, dict) else {}


@router.get("/order-schema")
async def order_schema(service_code: str = Query(...)):
    schema = get_order_schema(service_code)
    if not schema:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown service_code: {service_code}. Supported: {', '.join(list_schema_codes())}",
        )
    return schema


@router.post("/orders")
async def create_order(body: GxCreateOrder, background: BackgroundTasks):
    if (body.source or "").strip() != "gx-portal":
        raise HTTPException(status_code=422, detail="source must be gx-portal")
    order_id = (body.order_id or "").strip()
    if not order_id:
        raise HTTPException(status_code=422, detail="order_id is required")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$", order_id):
        raise HTTPException(status_code=422, detail="invalid order_id")

    r1 = (body.sample.fastq_r1_url or "").strip()
    r2 = (body.sample.fastq_r2_url or "").strip()
    if not r1 or not r2:
        raise HTTPException(status_code=422, detail="fastq_r1_url is required")

    daemon_code = resolve_daemon_service(body.service_code)
    if not daemon_code or not get_plugin(daemon_code):
        raise HTTPException(status_code=404, detail=f"Unknown service_code: {body.service_code}")

    org_type = (body.organization.type or "").strip().upper()
    if org_type not in ("CLIENT", "PARTNER"):
        raise HTTPException(status_code=422, detail="organization.type must be CLIENT or PARTNER")
    if not (body.callback.report_url or "").strip():
        raise HTTPException(status_code=422, detail="callback.report_url is required")

    qm = get_queue_manager()
    existing = qm.get_job(order_id)
    if existing and existing.status in _ACTIVE:
        logger.info("GX submit idempotent no-op for active order %s (%s)", order_id, existing.status.value)
        return {"order_id": order_id, "status": "accepted"}

    gx_meta = _gx_meta(body)
    params = map_daemon_params(
        daemon_code,
        body.service_data,
        body.organization.model_dump(),
        body.sample.model_dump(),
        gx_meta,
    )
    plugin = get_plugin(daemon_code)
    ok, err = plugin.validate_params(params, strict=False)
    if not ok:
        raise HTTPException(status_code=422, detail=err or "service_data validation failed")

    job = Job(
        order_id=order_id,
        service_code=daemon_code,
        sample_name=order_id,
        work_dir=now_kst_date_compact(),
        params=params,
    )
    if existing and existing.status not in _CAN_RESTART and existing.status not in _ACTIVE:
        # COMPLETED / REPORT_READY: keep artifacts, refresh GX callbacks only
        prev = dict(existing.params or {})
        prev["_gx"] = gx_meta
        existing.params = prev
        await qm.persist_job(existing)
        logger.info("GX submit updated callbacks for terminal order %s", order_id)
        return {"order_id": order_id, "status": "accepted"}

    await qm.save_job(job)
    background.add_task(_ingest_and_start, order_id, r1, r2)
    return {"order_id": order_id, "status": "accepted"}


async def _ingest_and_start(order_id: str, r1_url: str, r2_url: str) -> None:
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        return
    status_url = _job_gx(job).get("status_url")
    try:
        job.update_status(OrderStatus.DOWNLOADING, progress=5, message="Downloading FASTQ from GX Portal")
        await qm.persist_job(job)

        dest = job.fastq_dir or os.path.join(
            os.path.dirname(job.output_dir or ".") if job.output_dir else ".",
            "fastq",
        )
        if not job.fastq_dir:
            dest = os.path.join(os.getcwd(), "fastq", job.work_dir, job.order_id)
            # layout helpers already set job.fastq_dir on save
            dest = job.fastq_dir or dest

        logger.info("GX FASTQ download start %s", order_id)
        r1_path, r2_path = await download_pair(r1_url, r2_url, dest, order_id)
        job.fastq_r1_path = r1_path
        job.fastq_r2_path = r2_path
        job.fastq_r1_url = None
        job.fastq_r2_url = None
        meta = dict(_job_gx(job))
        meta["fastq_downloaded"] = True
        params = dict(job.params or {})
        params["_gx"] = meta
        job.params = params
        job.update_status(OrderStatus.SAVED, progress=15, message="FASTQ ready")
        await qm.persist_job(job)

        await qm.start_saved_job(order_id)
        logger.info("GX order %s queued after FASTQ download", order_id)
    except Exception as exc:
        msg = str(exc)
        logger.error("GX ingest failed for %s: %s (urls %s , %s)", order_id, msg, safe_url_for_log(r1_url), safe_url_for_log(r2_url))
        job = qm.get_job(order_id) or job
        job.update_status(OrderStatus.FAILED, progress=0, message=msg)
        job.error_log = msg
        await qm.persist_job(job)
        await notify_failed(status_url, order_id, msg)


@router.post("/orders/{order_id}/send-report")
async def send_order_report(order_id: str):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    meta = _job_gx(job)
    if not meta.get("report_url"):
        raise HTTPException(status_code=409, detail="Not a GX Portal order (no callback.report_url)")
    if job.status not in (OrderStatus.REPORT_READY, OrderStatus.COMPLETED):
        raise HTTPException(status_code=409, detail=f"Order status {job.status.value} cannot send report")
    pdf = _pick_report_pdf(job)
    if not pdf:
        raise HTTPException(status_code=400, detail="No Report_*.pdf found. Generate the report first.")
    try:
        result = await send_report_pdf(meta["report_url"], pdf, order_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result


def _pick_report_pdf(job: Job) -> Optional[str]:
    roots: List[str] = []
    if job.output_dir:
        roots.append(job.output_dir)
    best: Optional[str] = None
    best_mtime = -1.0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not re.match(r"^Report_.*\.pdf$", name, re.I):
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best = path
    return best
