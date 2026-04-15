"""
GX-Daemon — FastAPI Application

Headless genomics daemon combining:
- Platform-originated submit flow (nipt-daemon style)
- Carrier Screening pipeline + review APIs (service-daemon style)
- No embedded Portal UI
"""

import os
import json
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Body, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .datetime_kst import now_kst_iso, now_kst_date_compact
from .logging_config import setup_logging, setup_middleware
from .models import (
    OrderSubmitRequest, OrderSubmitResponse, OrderSaveResponse, OrderStatusResponse,
    OrderUpdateRequest, OrderUpdateResponse, StartOrderRequest,
    OrderStatus, Job, QueueSummary, OutputFile,
    ReportGenerateRequest, ReportGenerateResponse,
    GeneKnowledgeSaveRequest, VariantKnowledgeSaveRequest,
    UpdateFastqPathsRequest, DarkGenesReviewRequest, PgxReviewRequest,
    WesPanelCustomSave,
    SubmitOrderDto, OrderDetailSubmit, AnalysisStatus, FullOrder,
)
from .queue_manager import get_queue_manager
from .order_store import ingest_report_json_from_disk
from .annotation_resources import annotation_resource_report
from .runner import get_runner
from .platform_client import (
    get_platform_client, extract_work_dir, fetch_full_order,
)
from .notifier import notify_aws_result, notify_aws_failed, attach_analysis_file, upload_pdf_report
from .services import load_plugins, get_plugin, list_service_codes, get_all_plugins

logger = logging.getLogger(__name__)

_CARRIER_LIKE = frozenset({"carrier_screening", "whole_exome", "health_screening"})
_NIPT_TYPES = frozenset({"NIPT", "nipt", "sgnipt", "SGNIPT"})

_RESULT_JSON_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    load_plugins(settings.enabled_service_list)
    logger.info("GX-Daemon starting (%s)", settings.app_env)

    runner = get_runner()
    runner_task = asyncio.create_task(runner.start())

    yield

    runner._shutdown_event.set()
    await runner_task
    logger.info("GX-Daemon stopped")


app = FastAPI(
    title="GX-Daemon",
    description="Headless genomics analysis daemon (Carrier Screening + Platform integration)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
setup_middleware(app)

# ── Auth middleware ────────────────────────────────────────────

ACCESS_KEY = settings.api_key or ""
SKIP_PATHS = {"/", "/health", "/test", "/docs", "/redoc", "/openapi.json", "/queue/summary", "/static"}
SKIP_METHODS = {"OPTIONS"}


@app.middleware("http")
async def access_key_guard(request: Request, call_next):
    if (request.method in SKIP_METHODS or request.url.path in SKIP_PATHS):
        return await call_next(request)
    if not ACCESS_KEY:
        return await call_next(request)
    expected = f"Bearer {ACCESS_KEY}"
    received = request.headers.get("Authorization", "")
    api_key = request.headers.get("X-API-Key", "")
    if hmac.compare_digest(received, expected) or hmac.compare_digest(api_key, ACCESS_KEY):
        return await call_next(request)
    logger.warning("Forbidden request: invalid or missing Authorization header")
    return JSONResponse(status_code=403, content={"detail": "Forbidden"})


# ══════════════════════════════════════════════════════════════
# HEALTH / ROOT
# ══════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": settings.app_name,
        "env": settings.app_env,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/test")
async def test():
    """Liveness probe (nipt-daemon compatible)."""
    return {"status": "API is working"}


@app.get("/health")
async def health():
    qm = get_queue_manager()
    return {
        "status": "ok",
        "queue_size": qm._queue.qsize(),
        "running": len(qm._running_jobs),
        "max_concurrent": qm.max_concurrent,
        "available_slots": qm.available_slots,
    }


# ══════════════════════════════════════════════════════════════
# PLATFORM SUBMIT (nipt-daemon style)
# ══════════════════════════════════════════════════════════════

def _detect_service_code(dto_type: str) -> str:
    """Detect service_code from the Platform order type field."""
    if dto_type.upper() in _NIPT_TYPES:
        return "sgnipt"
    return "carrier_screening"


@app.post("/analysis/order/{order_id}/submit")
async def platform_submit_order(order_id: str, dto: SubmitOrderDto, background: BackgroundTasks):
    """Platform-originated order submit (nipt-daemon + carrier compatible)."""
    logger.info(f"Platform submit: {order_id}, type={dto.type}")
    qm = get_queue_manager()
    try:
        patient_age = dto.calculate_age()
        od = OrderDetailSubmit(
            id=order_id,
            clientId="",
            patientBirth=dto.patientBirthDate,
            age=patient_age,
            labIdentifier=dto.labIdentifier,
            sampleBarcode=dto.sampleBarcode,
        )

        service_code = _detect_service_code(dto.type)
        work_dir = extract_work_dir(order_id)

        job = Job(
            order_id=order_id,
            service_code=service_code,
            sample_name=order_id,
            work_dir=work_dir,
            params={
                "platform_submit": True,
                "patient_birth_date": dto.patientBirthDate,
                "sequencing_data_method": dto.sequencingDataMethod,
                "lab_identifier": dto.labIdentifier,
                "sample_barcode": dto.sampleBarcode,
                "type": dto.type,
                "patient_age": patient_age,
            },
        )

        if service_code == "sgnipt":
            background.add_task(_enqueue_nipt_order, qm, order_id, od, dto, job)
        else:
            await qm.enqueue(job)

        return {
            "message": "order received",
            "order_id": order_id,
            "patient_age": patient_age,
            "lab_identifier": dto.labIdentifier,
            "sample_barcode": dto.sampleBarcode,
            "status": "queued",
        }
    except Exception as e:
        logger.error(f"Error processing submit for {order_id}: {e}")
        raise HTTPException(500, f"Internal server error: {str(e)}")


async def _enqueue_nipt_order(qm, order_id: str, od, dto, job: Job):
    """Background task: fetch full order from platform (incl. FASTQ), then enqueue."""
    try:
        full_order: FullOrder = await fetch_full_order(order_id, od, dto)
        job.fastq_r1_path = getattr(full_order, "r1_path", None)
        job.fastq_r2_path = getattr(full_order, "r2_path", None)
        job.params["full_order"] = full_order.model_dump(mode="json")
        await qm.enqueue(job)
        logger.info(f"NIPT order {order_id} enqueued with FASTQ paths")
    except Exception as e:
        logger.error(f"Failed to enqueue NIPT order {order_id}: {e}")
        try:
            await notify_aws_failed(order_id, str(e))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# LOCAL SAVE / START (service-daemon style for local dev)
# ══════════════════════════════════════════════════════════════

@app.post("/order/{service_code}/save", response_model=OrderSaveResponse)
async def save_order(service_code: str, req: OrderSubmitRequest = Body(...)):
    """Save order without queueing (for local development / review)."""
    qm = get_queue_manager()
    if service_code not in settings.enabled_service_list:
        raise HTTPException(400, f"Unknown service_code: {service_code}")

    job = Job(
        order_id=req.order_id,
        service_code=service_code,
        sample_name=req.sample_name or req.order_id,
        work_dir=req.work_dir or now_kst_date_compact(),
        fastq_r1_url=req.fastq_r1_url,
        fastq_r2_url=req.fastq_r2_url,
        fastq_r1_path=req.fastq_r1_path,
        fastq_r2_path=req.fastq_r2_path,
        params=req.params or {},
    )
    await qm.save_job(job)
    return OrderSaveResponse(
        status="saved", order_id=req.order_id, service_code=service_code,
        message="Order saved (not queued)",
    )


@app.post("/order/{order_id}/start", response_model=OrderSubmitResponse)
async def start_order(order_id: str, body: StartOrderRequest = Body(StartOrderRequest())):
    """Start a saved/failed order."""
    qm = get_queue_manager()
    try:
        job, pos = await qm.start_saved_job(order_id, fresh=body.fresh)
    except KeyError:
        raise HTTPException(404, f"Order not found or not startable: {order_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return OrderSubmitResponse(
        status="queued", order_id=order_id, service_code=job.service_code,
        message="Order queued for execution", queue_position=pos,
    )


@app.post("/order/{service_code}/submit", response_model=OrderSubmitResponse)
async def submit_order(service_code: str, req: OrderSubmitRequest = Body(...)):
    """Save + immediately queue an order (local API)."""
    qm = get_queue_manager()
    if service_code not in settings.enabled_service_list:
        raise HTTPException(400, f"Unknown service_code: {service_code}")

    job = Job(
        order_id=req.order_id,
        service_code=service_code,
        sample_name=req.sample_name or req.order_id,
        work_dir=req.work_dir or now_kst_date_compact(),
        fastq_r1_url=req.fastq_r1_url,
        fastq_r2_url=req.fastq_r2_url,
        fastq_r1_path=req.fastq_r1_path,
        fastq_r2_path=req.fastq_r2_path,
        params=req.params or {},
    )
    queue_position = await qm.enqueue(job)
    return OrderSubmitResponse(
        status="queued", order_id=req.order_id, service_code=service_code,
        message="Order queued for execution", queue_position=queue_position,
    )


# ══════════════════════════════════════════════════════════════
# ORDER STATUS / LIST
# ══════════════════════════════════════════════════════════════

@app.get("/order/{order_id}/status", response_model=OrderStatusResponse)
async def get_order_status(order_id: str):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")
    return OrderStatusResponse(
        order_id=job.order_id,
        service_code=job.service_code,
        status=job.status,
        progress=job.progress,
        message=job.message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/status/{order_id}")
async def get_status_compat(order_id: str):
    """nipt-daemon compatible status endpoint."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        return AnalysisStatus(orderId=order_id, status="not_found")
    status_map = {
        OrderStatus.QUEUED: "queued",
        OrderStatus.RUNNING: "running",
        OrderStatus.COMPLETED: "success",
        OrderStatus.REPORT_READY: "success",
        OrderStatus.FAILED: "failure",
        OrderStatus.CANCELLED: "cancelled",
    }
    return AnalysisStatus(
        orderId=order_id,
        status=status_map.get(job.status, job.status.value.lower()),
    )


@app.get("/order/{order_id}")
async def get_order(order_id: str):
    """Get full order details."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")
    return job.model_dump(mode="json")


@app.get("/orders")
async def list_orders(
    service_code: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    qm = get_queue_manager()
    jobs = qm.iter_all_jobs_unique()
    if service_code:
        jobs = [j for j in jobs if j.service_code == service_code]
    if status:
        jobs = [j for j in jobs if j.status.value.upper() == status.upper()]
    return [j.model_dump(mode="json") for j in jobs]


# ══════════════════════════════════════════════════════════════
# RESULT / REVIEW
# ══════════════════════════════════════════════════════════════

@app.get("/order/{order_id}/result")
async def get_order_result(order_id: str, force_disk: bool = Query(False)):
    """Return result.json for review."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    store = qm.store
    result_data = None

    if store and not force_disk:
        result_data = store.get_result_json(order_id)

    if result_data is None:
        if job.service_code in _CARRIER_LIKE:
            from app.services.carrier_screening.plugin import carrier_result_json_path
            path = carrier_result_json_path(job)
        elif job.output_dir:
            path = os.path.join(job.output_dir, "result.json")
        else:
            path = None

        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                result_data = json.load(f)
            if store:
                store.set_result_json(order_id, result_data)

    if result_data is None:
        raise HTTPException(404, f"Result not available for {order_id}")

    return JSONResponse(content=result_data, headers=_RESULT_JSON_CACHE_HEADERS)


@app.post("/order/{order_id}/report", response_model=ReportGenerateResponse)
async def generate_report(order_id: str, req: ReportGenerateRequest = Body(...)):
    """Generate PDF report from reviewed variants."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    plugin = get_plugin(job.service_code)
    if not plugin:
        raise HTTPException(400, f"No plugin for service: {job.service_code}")

    if not hasattr(plugin, "generate_report"):
        raise HTTPException(400, f"Plugin {job.service_code} does not support report generation")

    try:
        report_files = await plugin.generate_report(
            job,
            confirmed_variants=req.confirmed_variants,
            reviewer_info=req.reviewer_info,
            patient_info=req.patient_info,
            partner_info=req.partner_info,
            languages=req.languages or settings.report_language_list,
        )
    except Exception as e:
        logger.exception("Report generation raised for order %s", order_id)
        msg = str(e).strip() or repr(e)
        raise HTTPException(status_code=500, detail=msg[:8000]) from e

    if not report_files:
        raise HTTPException(
            status_code=500,
            detail="Report generation failed (plugin returned false — check daemon logs).",
        )

    if qm.store:
        ingest_report_json_from_disk(qm.store, job)

    await qm.mark_report_ready(order_id)

    return ReportGenerateResponse(
        status="success",
        order_id=order_id,
        service_code=job.service_code,
        report_files=report_files,
        message="Report generated successfully",
    )


@app.post("/analysis/order/{order_id}/report")
async def platform_generate_report(order_id: str, request: Request):
    """Generate report and upload to Platform (nipt-daemon compatible)."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    try:
        body = await request.json()
        plugin = get_plugin(job.service_code)
        if not plugin or not hasattr(plugin, "generate_report"):
            raise HTTPException(400, f"No report support for {job.service_code}")

        report_files = await plugin.generate_report(
            job,
            confirmed_variants=body.get("confirmed_variants", []),
            reviewer_info=body.get("reviewer_info", {}),
            patient_info=body.get("patient_info"),
            partner_info=body.get("partner_info"),
            languages=body.get("languages") or settings.report_language_list,
        )

        platform_client = get_platform_client()
        uploaded = 0
        for pdf_path in report_files:
            if pdf_path.endswith(".pdf") and os.path.exists(pdf_path):
                result = await platform_client.upload_pdf_report(order_id, pdf_path)
                if result.status.value == "SUCCESS":
                    uploaded += 1

        await qm.mark_report_ready(order_id)

        return {
            "order_id": order_id,
            "report_files": report_files,
            "uploaded_count": uploaded,
            "status": "success",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Platform report generation failed for {order_id}: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════
# DARK GENES / PGX REVIEW
# ══════════════════════════════════════════════════════════════

@app.post("/order/{order_id}/dark-genes-review")
async def save_dark_genes_review(order_id: str, req: DarkGenesReviewRequest = Body(...)):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    store = qm.store
    if not store:
        raise HTTPException(500, "No persistence store")

    result_data = store.get_result_json(order_id)
    if not result_data:
        raise HTTPException(404, "result.json not found in store")

    dg = result_data.get("dark_genes") or {}
    dg["section_reviews"] = [r.model_dump() for r in req.section_reviews]
    result_data["dark_genes"] = dg

    store.set_result_json(order_id, result_data)
    return {"status": "saved", "order_id": order_id}


@app.post("/order/{order_id}/pgx-review")
async def save_pgx_review(order_id: str, req: PgxReviewRequest = Body(...)):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    store = qm.store
    if not store:
        raise HTTPException(500, "No persistence store")

    result_data = store.get_result_json(order_id)
    if not result_data:
        raise HTTPException(404, "result.json not found in store")

    pgx = result_data.get("pgx") or {}
    prev_pr = pgx.get("portal_review") if isinstance(pgx.get("portal_review"), dict) else {}
    pgx["portal_review"] = {
        **prev_pr,
        "reviewer_notes": (req.reviewer_notes or "")[:16000],
        "reviewed": bool(req.reviewed),
        "include_apoe_proactive_pdf": bool(req.include_apoe_proactive_pdf),
    }

    gene_results = pgx.get("gene_results") or []
    review_map = {r.gene: r for r in req.gene_reviews}
    for gr in gene_results:
        gene = gr.get("gene", "")
        if gene in review_map:
            rv = review_map[gene]
            gr["reviewer_confirmed"] = rv.reviewer_confirmed
            gr["reviewer_comment"] = rv.reviewer_comment

    custom_results = pgx.get("custom_gene_results") or []
    custom_map = {(r.gene, r.rsid): r for r in req.custom_gene_reviews}
    for cr in custom_results:
        key = (cr.get("gene", ""), cr.get("rsid", ""))
        if key in custom_map:
            u = custom_map[key]
            cr["reviewer_confirmed"] = bool(u.reviewer_confirmed)
            cr["reviewer_comment"] = (u.reviewer_comment or "")[:4000]

    pgx["gene_results"] = gene_results
    pgx["custom_gene_results"] = custom_results
    result_data["pgx"] = pgx

    store.set_result_json(order_id, result_data)
    return {"status": "saved", "order_id": order_id}


# ══════════════════════════════════════════════════════════════
# GENE / VARIANT KNOWLEDGE
# ══════════════════════════════════════════════════════════════

@app.put("/order/{order_id}/gene-knowledge")
async def save_gene_knowledge(order_id: str, req: GeneKnowledgeSaveRequest = Body(...)):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")
    try:
        from app.services.carrier_screening.gene_knowledge_db import save_gene_data
        save_gene_data(req.gene, req.model_dump())
        return {"status": "saved", "gene": req.gene}
    except ImportError:
        raise HTTPException(500, "gene_knowledge_db module not available")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.put("/order/{order_id}/variant-knowledge")
async def save_variant_knowledge(order_id: str, req: VariantKnowledgeSaveRequest = Body(...)):
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")
    try:
        from app.services.carrier_screening.gene_knowledge_db import save_variant_data
        save_variant_data(req.variant_key, req.variant_notes)
        return {"status": "saved", "variant_key": req.variant_key}
    except ImportError:
        raise HTTPException(500, "gene_knowledge_db module not available")
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════
# QUEUE / SERVICES
# ══════════════════════════════════════════════════════════════

@app.get("/queue/summary", response_model=QueueSummary)
async def queue_summary():
    qm = get_queue_manager()
    return qm.get_summary()


@app.get("/queue/status")
async def queue_status():
    qm = get_queue_manager()
    return {
        "queue_size": qm._queue.qsize(),
        "running": {j.order_id: j.model_dump(mode="json") for j in qm._running_jobs.values()},
        "max_concurrent": qm.max_concurrent,
        "available_slots": qm.available_slots,
    }


@app.get("/services")
async def list_services():
    plugins = get_all_plugins()
    return {
        "enabled": settings.enabled_service_list,
        "plugins": {
            code: {
                "display_name": p.display_name,
                "service_code": p.service_code,
            }
            for code, p in plugins.items()
        },
    }


@app.get("/api/resources")
async def resources():
    return annotation_resource_report()


# ══════════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ══════════════════════════════════════════════════════════════

@app.post("/order/{order_id}/stop")
async def stop_order(order_id: str):
    """Stop a running order."""
    qm = get_queue_manager()
    runner = get_runner()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    if job.status == OrderStatus.QUEUED:
        ok = await qm.request_cancel_queued(order_id)
        return {"status": "cancel_requested" if ok else "not_queued", "order_id": order_id}

    if job.status in (OrderStatus.RUNNING, OrderStatus.DOWNLOADING, OrderStatus.PROCESSING, OrderStatus.UPLOADING):
        killed = await runner.cancel_job(order_id)
        if not killed:
            runner.record_stop_request(order_id)
        return {"status": "stop_requested", "order_id": order_id, "pid_killed": killed}

    return {"status": "not_active", "order_id": order_id, "current_status": job.status.value}


@app.post("/order/{order_id}/delete-run")
async def delete_order_run(order_id: str):
    """Delete order and its artifacts."""
    qm = get_queue_manager()
    ok, msg, detail = await qm.delete_order_with_artifacts(order_id)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "deleted", "message": msg, "detail": detail}


@app.post("/order/{order_id}/reprocess-results")
async def reprocess_results(order_id: str):
    """Re-run result processing (annotation, result.json) without re-running the pipeline."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(404, f"Order not found: {order_id}")

    plugin = get_plugin(job.service_code)
    if not plugin:
        raise HTTPException(400, f"No plugin for {job.service_code}")

    try:
        ok = await plugin.process_results(job)
        if not ok:
            hint = (getattr(job, "error_log", None) or "").strip()
            detail = (
                f"process_results failed: {hint}"
                if hint
                else "process_results failed — see daemon logs"
            )
            raise HTTPException(status_code=500, detail=detail)
        await qm.finalize_reprocess_results(job)
        return {"status": "ok", "order_id": order_id, "message": "Results reprocessed"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reprocess failed for {order_id}: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════
# NIPT-DAEMON BACKWARD COMPATIBILITY
# ══════════════════════════════════════════════════════════════

@app.get("/status/{order_id}/progress")
async def get_order_progress(order_id: str):
    """Pipeline progress file (nipt-daemon compatible)."""
    try:
        work_dir = extract_work_dir(order_id)
        progress_file = os.path.join(
            settings.nipt_output_dir, work_dir, order_id, f"{order_id}_progress.txt"
        )
        qm = get_queue_manager()
        job = qm.get_job(order_id)
        status = job.status.value if job else "not_found"

        progress_content = ""
        file_exists = os.path.exists(progress_file)
        if file_exists:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress_content = f.read()

        return {
            "order_id": order_id,
            "status": status,
            "progress_file_exists": file_exists,
            "progress_content": progress_content,
            "last_updated": os.path.getmtime(progress_file) if file_exists else None,
        }
    except Exception as e:
        return {
            "order_id": order_id,
            "status": "error",
            "progress_file_exists": False,
            "progress_content": f"Error reading progress: {str(e)}",
            "last_updated": None,
        }


@app.get("/status")
async def get_all_status():
    """System status overview (nipt-daemon compatible alias)."""
    return await queue_summary()


# ── NIPT Report Generation (using nipt-daemon report_functions) ──

@app.post("/analysis/order/{order_id}/nipt-report")
async def generate_nipt_report(order_id: str, request: Request):
    """
    Generate NIPT report and upload to Platform.
    Engine is selected via NIPT_REPORT_ENGINE env var: 'pptx' (default) or 'html'.
    """
    try:
        body = await request.json()
        engine = (body.get("engine") or settings.nipt_report_engine).strip().lower()
        logger.info(f"Generating NIPT report for order {order_id} [engine={engine}]")

        from .report_functions import make_report_json, generate_from_json

        work_dir = extract_work_dir(order_id)
        output_dir = os.path.join(
            settings.nipt_output_dir, work_dir, order_id
        )

        report_json = await make_report_json(order_id, body)

        os.makedirs(output_dir, exist_ok=True)
        report_json_path = os.path.join(output_dir, f"{order_id}_report.json")
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report_json, f, indent=2, ensure_ascii=False, default=str)

        if engine == "html":
            from .nipt_report_html import generate_html_report

            result = generate_html_report(
                report_json,
                output_dir,
                order_options=body.get("order_options"),
            )
        else:
            template_dir = settings.report_temp_dir
            result = generate_from_json(report_json, template_dir, output_dir)

        pdf_path = result.get("merged")

        if not pdf_path or not os.path.exists(pdf_path):
            error_msg = "PDF generation failed"
            logger.error(f"{error_msg} for {order_id}")
            await notify_aws_failed(order_id, error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        upload_success = await upload_pdf_report(order_id, pdf_path, signed=False)
        if not upload_success:
            error_msg = "PDF upload failed"
            logger.error(f"{error_msg} for {order_id}")
            await notify_aws_failed(order_id, error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        logger.info(f"NIPT report generation completed for {order_id} [engine={engine}]")
        return {
            "order_id": order_id,
            "engine": engine,
            "pdf": pdf_path,
            "html": result.get("html"),
            "individual_pdfs": result.get("individual"),
            "upload_status": "success",
        }
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Report generation error: {str(e)}"
        logger.error(f"{error_msg} for {order_id}")
        await notify_aws_failed(order_id, error_msg)
        raise HTTPException(status_code=500, detail=error_msg)


@app.post("/analysis/order/{order_id}/sign-report")
async def generate_sign_report(order_id: str):
    """Sign existing PDF and upload as signed report (nipt-daemon compatible)."""
    try:
        logger.info(f"[sign+upload] Start for order_id={order_id}")

        from .pdf_tools import sign_pdf_with_caption_pt

        work_dir = extract_work_dir(order_id)
        output_dir = os.path.join(
            settings.nipt_output_dir, work_dir, order_id
        )

        report_json_path = os.path.join(output_dir, f"{order_id}_report.json")
        if not os.path.exists(report_json_path):
            raise HTTPException(status_code=404, detail=f"Report JSON not found: {report_json_path}")

        with open(report_json_path, "r", encoding="utf-8") as f:
            report_json = json.load(f)

        sample_id = str(report_json.get("Sample ID", "None")).strip().replace(" ", "_")
        if not sample_id:
            raise HTTPException(status_code=400, detail="sample_id not found in report JSON")

        base_pdf_path = os.path.join(output_dir, f"{order_id}_{sample_id}.pdf")
        if not os.path.exists(base_pdf_path):
            raise HTTPException(
                status_code=404,
                detail=f"Base PDF not found: {base_pdf_path}",
            )

        signature_dir = settings.report_sign_dir
        signature_img = os.path.join(signature_dir, "signature.png")
        if not os.path.exists(signature_img):
            raise HTTPException(status_code=500, detail=f"Signature image not found: {signature_img}")

        signer = "Yuyus Kusnadi, PhD"
        reason = "e-signature"

        sign_kwargs = dict(
            sig_img_path=signature_img,
            x_pt=None, y_pt=None, w_pt=None,
            signer=signer, reason=reason, show_date=True,
            font_size=6.0, caption_pos="right",
            caption_gap_pt=3.0, caption_overlap_pt=0.0,
            caption_dx_pt=0.0, caption_dy_pt=0.0,
            trim=True, white_threshold=250,
            width_ratio=0.10, anchor="bottom_right",
            margin_w_ratio=0.2, margin_h_ratio=0.01,
            anchor_includes_caption=True,
        )

        first_signed = sign_pdf_with_caption_pt(
            input_pdf=base_pdf_path, page_index=0,
            out_path=base_pdf_path, overwrite=True, **sign_kwargs,
        )

        signed_pdf = sign_pdf_with_caption_pt(
            input_pdf=first_signed, page_index=1,
            out_path=None, overwrite=True, **sign_kwargs,
        )

        ok = await upload_pdf_report(order_id, signed_pdf, signed=True)
        if not ok:
            raise HTTPException(status_code=500, detail="Signed PDF upload failed")

        return {
            "order_id": order_id,
            "base_pdf": base_pdf_path,
            "signed_pdf": signed_pdf,
            "upload_status": "success",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in sign-report for {order_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# DEBUG API
# ══════════════════════════════════════════════════════════════

@app.get("/debug/runner")
async def get_runner_status():
    """Runner / worker status."""
    runner = get_runner()
    qm = get_queue_manager()
    return {
        "workers": runner._worker_count if hasattr(runner, "_worker_count") else settings.max_concurrent_jobs,
        "running_pids": {oid: pid for oid, pid in getattr(runner, "_running_pids", {}).items()},
        "queue_manager_running": list(qm._running_jobs.keys()),
    }


@app.post("/debug/mark_completed/{order_id}")
async def mark_completed_debug(order_id: str, success: bool = True):
    """Manually mark an order as completed (debug)."""
    qm = get_queue_manager()
    message = "Manually marked as completed"
    await qm.mark_completed(order_id, success=success)
    return {"message": f"Marked {order_id} as {'completed' if success else 'failed'}"}


@app.post("/debug/check_completion/{order_id}")
async def check_completion_debug(order_id: str):
    """Check and process completion for an order (debug)."""
    try:
        work_dir = extract_work_dir(order_id)
        json_file = os.path.join(
            settings.nipt_output_dir, work_dir, order_id, f"{order_id}.json"
        )
        tar_file = os.path.join(
            settings.nipt_output_dir, work_dir, order_id, f"{order_id}.output.tar"
        )

        result = {
            "order_id": order_id,
            "json_exists": os.path.exists(json_file),
            "tar_exists": os.path.exists(tar_file),
            "json_path": json_file,
            "tar_path": tar_file,
        }

        if os.path.exists(json_file):
            qm = get_queue_manager()
            await qm.mark_completed(order_id, success=True)
            result["action"] = "Marked as completed"
        else:
            result["action"] = "No action taken - JSON file not found"

        return result
    except Exception as e:
        return {"error": str(e)}
