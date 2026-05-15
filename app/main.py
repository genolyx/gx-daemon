"""
GX-Daemon — FastAPI Application

Headless genomics daemon combining:
- Platform-originated submit flow (nipt-daemon style)
- Carrier Screening pipeline + review APIs (service-daemon style)
- No embedded Portal UI
"""

import os
import re
import json
import asyncio
import csv as _csv
import glob
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, HTTPException, Query, Body, Request, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
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
from .services.carrier_screening.prior_reuse import prior_reuse_artifact_roots

logger = logging.getLogger(__name__)

_CARRIER_LIKE = frozenset({"carrier_screening", "whole_exome", "health_screening"})
# Platform order ``type`` strings that should route to the NIPT pipeline
# (gx-nipt / Nextflow). Keep sgNIPT (single-gene) strings as a separate
# cluster so they go to the sgnipt plugin.
_NIPT_TYPES   = frozenset({"NIPT", "nipt"})
_SGNIPT_TYPES = frozenset({"SGNIPT", "sgnipt", "SG_NIPT", "sg-nipt"})

_RESULT_JSON_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}

_FASTQ_NAME_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")


# ── FASTQ / BAM-CSV browse helpers ────────────────────────────

def _fastq_root_for_service(service_code: Optional[str]) -> str:
    sc = (service_code or "").strip().lower().replace("-", "_")
    if sc == "sgnipt":
        root = getattr(settings, "sgnipt_fastq_root", None) or settings.fastq_base_dir
        return os.path.realpath(root)
    if sc in ("carrier_screening", "whole_exome", "health_screening"):
        d = getattr(settings, "carrier_screening_fastq_dir", None) or settings.fastq_base_dir
        return os.path.realpath(d)
    return os.path.realpath(settings.fastq_base_dir)


def _normalize_rel_path(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    parts: List[str] = []
    for p in rel.split("/"):
        if not p or p == ".":
            continue
        if p == "..":
            raise HTTPException(status_code=400, detail="Invalid path segment")
        parts.append(p)
    return "/".join(parts)


def _safe_join_under_fastq(rel: str, service_code: Optional[str] = None) -> str:
    root = _fastq_root_for_service(service_code)
    norm = _normalize_rel_path(rel)
    target = os.path.realpath(os.path.join(root, norm) if norm else root)
    root_sep = root if root.endswith(os.sep) else root + os.sep
    if target != root and not target.startswith(root_sep):
        raise HTTPException(status_code=400, detail="Path escapes FASTQ root")
    return target


def _is_fastq_filename(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(s) for s in _FASTQ_NAME_SUFFIXES)


def _is_csv_filename(name: str) -> bool:
    return name.lower().endswith(".csv")


def _bam_csv_root_for_service(service_code: Optional[str]) -> str:
    sc = (service_code or "").strip().lower().replace("-", "_")
    if sc in ("carrier_screening", "whole_exome", "health_screening"):
        wr = getattr(settings, "carrier_screening_work_root", None) or settings.base_dir
        return os.path.realpath(os.path.join(wr, "data"))
    data_dir = (getattr(settings, "sgnipt_data_dir", None) or "").strip()
    if data_dir:
        return os.path.realpath(data_dir)
    return os.path.realpath(os.path.join(getattr(settings, "sgnipt_job_root", settings.base_dir), "data"))


def _browse_fastq_directory_payload(path: str, service_code: Optional[str]) -> Dict[str, Any]:
    root = _fastq_root_for_service(service_code)
    norm_prefix = _normalize_rel_path(path)

    if not norm_prefix and not os.path.isdir(root):
        return {
            "root": root, "rel_path": "", "parent_rel": "", "root_exists": False,
            "service_code": service_code, "items": [],
            "hint": f"FASTQ root does not exist: {root}. Check FASTQ_BASE_DIR in .env.",
        }

    target = _safe_join_under_fastq(path, service_code)
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail=f"Path not found: {norm_prefix or '(root)'}")
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail="Not a directory")

    items: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(target))
    except OSError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        rel_child = f"{norm_prefix}/{name}" if norm_prefix else name
        rel_child = rel_child.replace("\\", "/")
        if os.path.isdir(full):
            items.append({"name": name, "rel_path": rel_child, "kind": "dir"})
        elif os.path.isfile(full) and _is_fastq_filename(name):
            items.append({"name": name, "rel_path": rel_child, "abs_path": full, "kind": "file"})

    parent_rel = "/".join(norm_prefix.split("/")[:-1]) if norm_prefix else ""
    return {
        "root": root, "rel_path": norm_prefix, "parent_rel": parent_rel,
        "root_exists": True, "service_code": service_code, "items": items,
    }


def _browse_bam_csv_directory_payload(
    path: str,
    service_code: Optional[str],
    abs_path: Optional[str] = None,
) -> Dict[str, Any]:
    default_root = _bam_csv_root_for_service(service_code)
    if abs_path:
        target = os.path.realpath(abs_path.strip())
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail=f"Path not found: {abs_path}")
        if not os.path.isdir(target):
            raise HTTPException(status_code=400, detail=f"Not a directory: {abs_path}")
        root = target
        norm_prefix = ""
        parent_abs = os.path.dirname(target)
    else:
        root = default_root
        norm_prefix = _normalize_rel_path(path)
        if not norm_prefix and not os.path.isdir(root):
            return {
                "root": root, "rel_path": "", "parent_rel": "",
                "root_exists": False, "service_code": service_code, "items": [],
            }
        target = os.path.realpath(os.path.join(root, norm_prefix) if norm_prefix else root)
        root_sep = root if root.endswith(os.sep) else root + os.sep
        if target != root and not target.startswith(root_sep):
            raise HTTPException(status_code=400, detail="Path escapes BAM data root")
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail=f"Path not found: {norm_prefix or '(root)'}")
        if not os.path.isdir(target):
            raise HTTPException(status_code=400, detail="Not a directory")
        parent_abs = os.path.dirname(target) if norm_prefix else None

    items: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(target))
    except OSError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        if abs_path:
            if os.path.isdir(full):
                items.append({"name": name, "kind": "dir", "abs_path": full})
            elif os.path.isfile(full) and _is_csv_filename(name):
                items.append({"name": name, "abs_path": full, "kind": "file"})
        else:
            rel_child = f"{norm_prefix}/{name}" if norm_prefix else name
            rel_child = rel_child.replace("\\", "/")
            if os.path.isdir(full):
                items.append({"name": name, "kind": "dir", "rel_path": rel_child})
            elif os.path.isfile(full) and _is_csv_filename(name):
                items.append({"name": name, "abs_path": full, "rel_path": rel_child, "kind": "file"})

    if abs_path:
        return {
            "root": root, "current_abs": target, "parent_abs": parent_abs,
            "rel_path": "", "parent_rel": "", "root_exists": True,
            "service_code": service_code, "items": items,
        }
    parent_rel = "/".join(norm_prefix.split("/")[:-1]) if norm_prefix else ""
    return {
        "root": root, "current_abs": target, "rel_path": norm_prefix, "parent_rel": parent_rel,
        "root_exists": True, "service_code": service_code, "items": items,
    }


def _validate_optional_fastq_file(abs_path: str, service_code: str) -> str:
    full = os.path.realpath(abs_path.strip())
    if not os.path.isfile(full):
        raise HTTPException(status_code=400, detail=f"Not a file: {abs_path}")
    root = _fastq_root_for_service(service_code)
    root_sep = root if root.endswith(os.sep) else root + os.sep
    if full != root and not full.startswith(root_sep):
        raise HTTPException(status_code=400, detail=f"Path is outside FASTQ root for {service_code}")
    return full


# ── Job build helpers ──────────────────────────────────────────

def _pipeline_folder_label(order_id: str, sample_name: Optional[str]) -> str:
    s = (sample_name or "").strip()
    return s if s else (order_id or "")


def _build_job_from_submit_request(service_code: str, request) -> Job:
    work_dir = (request.work_dir or "").strip() or now_kst_date_compact()
    folder = _pipeline_folder_label(request.order_id, request.sample_name)
    if service_code == "sgnipt":
        root = getattr(settings, "sgnipt_job_root", settings.base_dir)
        oid = (request.order_id or "").strip()
        return Job(
            order_id=request.order_id, service_code=service_code, sample_name=folder,
            work_dir=work_dir,
            fastq_r1_url=request.fastq_r1_url, fastq_r2_url=request.fastq_r2_url,
            fastq_r1_path=request.fastq_r1_path, fastq_r2_path=request.fastq_r2_path,
            params=request.params or {},
            fastq_dir=os.path.join(root, "fastq", work_dir, oid),
            analysis_dir=os.path.join(root, "analysis", work_dir, oid),
            output_dir=os.path.join(root, "output", work_dir, oid),
            log_dir=os.path.join(root, "log", work_dir, oid),
        )
    if service_code in ("carrier_screening", "whole_exome", "health_screening"):
        work_root = getattr(settings, "carrier_screening_work_root", settings.base_dir)
        return Job(
            order_id=request.order_id, service_code=service_code, sample_name=folder,
            work_dir=work_dir,
            fastq_r1_url=request.fastq_r1_url, fastq_r2_url=request.fastq_r2_url,
            fastq_r1_path=request.fastq_r1_path, fastq_r2_path=request.fastq_r2_path,
            params=request.params or {},
            fastq_dir=os.path.join(work_root, "fastq", work_dir, folder),
            analysis_dir=os.path.join(work_root, "analysis", work_dir, folder),
            output_dir=os.path.join(work_root, "output", work_dir, folder),
            log_dir=os.path.join(work_root, "log", work_dir, folder),
        )
    base = settings.base_dir
    return Job(
        order_id=request.order_id, service_code=service_code, sample_name=folder,
        work_dir=work_dir,
        fastq_r1_url=request.fastq_r1_url, fastq_r2_url=request.fastq_r2_url,
        fastq_r1_path=request.fastq_r1_path, fastq_r2_path=request.fastq_r2_path,
        params=request.params or {},
        fastq_dir=os.path.join(base, "fastq", work_dir, folder),
        analysis_dir=os.path.join(base, "analysis", work_dir, folder),
        output_dir=os.path.join(base, "output", work_dir, folder),
        log_dir=os.path.join(base, "log", work_dir, folder),
    )


def _merge_order_params(old: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(old or {})
    for k, v in patch.items():
        if k in ("carrier", "nipt") and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _order_merge_submit(job: Job, patch) -> Any:
    from .models import OrderSubmitRequest as _OSR
    data = patch.model_dump(exclude_unset=True)
    new_oid = data.pop("order_id", None)
    params_patch = data.pop("params", None)
    merged_params = dict(job.params or {})
    if params_patch is not None:
        merged_params = _merge_order_params(merged_params, params_patch)
    base: Dict[str, Any] = {
        "order_id": job.order_id, "service_code": job.service_code,
        "sample_name": job.sample_name, "work_dir": job.work_dir,
        "fastq_r1_url": job.fastq_r1_url, "fastq_r2_url": job.fastq_r2_url,
        "fastq_r1_path": job.fastq_r1_path, "fastq_r2_path": job.fastq_r2_path,
        "params": merged_params,
    }
    for k, v in data.items():
        if v is not None:
            base[k] = v
    if new_oid is not None and str(new_oid).strip():
        base["order_id"] = str(new_oid).strip()
    return _OSR(**base)


# ── Artifact / file helpers ────────────────────────────────────

def _order_artifact_roots(job: Job) -> List[str]:
    roots: List[str] = []
    roots.extend(prior_reuse_artifact_roots(job))
    if (job.service_code or "").strip() in _CARRIER_LIKE:
        from .services.carrier_screening.plugin import carrier_report_output_dir
        try:
            cro = carrier_report_output_dir(job)
            if cro:
                roots.append(cro)
        except Exception:
            pass
        ad = (job.analysis_dir or "").strip()
        if ad:
            try:
                if os.path.isdir(ad):
                    roots.append(ad)
            except OSError:
                pass
    if job.output_dir:
        roots.append(job.output_dir)
    if (job.service_code or "").strip() in _CARRIER_LIKE:
        from .services.carrier_screening.layout_norm import carrier_sequencing_folder
        wk = str(job.work_dir).strip() or "00"
        smp = str(job.sample_name).strip()
        seq = carrier_sequencing_folder(job)
        layout_base = (getattr(settings, "carrier_screening_layout_base", None) or "").strip()
        if layout_base and smp:
            roots.append(os.path.join(layout_base, "output", wk, smp))
        if seq and seq != smp and layout_base:
            roots.append(os.path.join(layout_base, "output", wk, seq))
        sd = (getattr(settings, "carrier_screening_script_data_dir", None) or "").strip()
        if sd and smp:
            roots.append(os.path.join(sd, "output", wk, smp))
        if sd and seq and seq != smp:
            roots.append(os.path.join(sd, "output", wk, seq))

    seen: set = set()
    out: List[str] = []
    for root in roots:
        if not root:
            continue
        try:
            key = os.path.realpath(root)
        except OSError:
            continue
        if not os.path.isdir(root):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    try:
        from .services.gene_panel_coverage import _coverage_search_roots
        for root in _coverage_search_roots(job):
            if not root:
                continue
            try:
                key = os.path.realpath(root)
            except OSError:
                continue
            if not os.path.isdir(root):
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(root)
    except Exception:
        pass
    return out


def _safe_order_file_path(root: str, rel: str) -> Optional[str]:
    if not rel or not root or ".." in rel.replace("\\", "/"):
        return None
    rel = rel.strip().lstrip("/").replace("\\", "/")
    try:
        root_abs = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
    except OSError:
        return None
    if full != root_abs and not full.startswith(root_abs + os.sep):
        return None
    return full if os.path.isfile(full) else None


def _resolve_order_artifact_path(job: Job, filename: str) -> Optional[str]:
    rel = (filename or "").strip().lstrip("/")
    if not rel or ".." in rel.replace("\\", "/"):
        return None
    hits: List[str] = []
    for root in _order_artifact_roots(job):
        hit = _safe_order_file_path(root, rel)
        if hit:
            hits.append(hit)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    try:
        return max(hits, key=lambda p: os.path.getmtime(p))
    except OSError:
        return hits[0]


def _resolve_order_file_or_404(order_id: str, filename: str) -> str:
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    file_path = _resolve_order_artifact_path(job, filename)
    if not file_path:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    return file_path


def _guess_file_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {"json": "json", "tsv": "tsv", "csv": "csv", "pdf": "pdf", "html": "html",
            "png": "image", "jpg": "image", "jpeg": "image", "vcf": "vcf", "bed": "bed"}.get(ext, "other")


def _guess_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "json": "application/json", "tsv": "text/tab-separated-values", "csv": "text/csv",
        "pdf": "application/pdf", "html": "text/html", "png": "image/png",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml", "webp": "image/webp",
        "bam": "application/octet-stream", "bai": "application/octet-stream",
        "vcf": "text/plain", "bed": "text/plain",
    }.get(ext, "application/octet-stream")


# ── BAM track helpers ──────────────────────────────────────────

_ANCILLARY_BAM_TOKENS = (
    "paraphase", "eh_realigned", "fmr1", "fragile", "frax",
    "smn_combined", "smn_merged", "smn_unified",
    "smn1_realigned", "smn2_realigned", "smn1_aln", "smn2_aln",
    "smn1_tagged", "smn2_tagged", "smn_raw",
    "strc_aln", "strc_realigned", "strc_gene2_realigned",
    "pms2_aln", "pms2_realigned", "gba_aln", "gba_realigned", "gba_tagged",
    "hba_aln", "hba_realigned", "no_dup", "nodup", "no-dup", "pre_dedup", "before_dedup",
)
_ANCILLARY_BAM_SUFFIXES = ("_realigned_old.bam", "_realigned_tagged.bam")


def _is_ancillary_bam(basename: str) -> bool:
    b = basename.lower()
    return any(tok in b for tok in _ANCILLARY_BAM_TOKENS) or any(b.endswith(s) for s in _ANCILLARY_BAM_SUFFIXES)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _annotate_bam_track_ancillary(job: Job, track: Dict[str, Any]) -> Dict[str, Any]:
    rel = (track.get("rel_path") or "").strip()
    if rel and _is_ancillary_bam(os.path.basename(rel)):
        track = dict(track)
        track["ancillary"] = True
    return track


def _order_file_rel_from_abs_bam(job: Job, bam_abs: str) -> Optional[Dict[str, Any]]:
    try:
        bam_abs = os.path.realpath(bam_abs)
    except OSError:
        return None
    if not os.path.isfile(bam_abs) or not bam_abs.lower().endswith(".bam"):
        return None
    for root in _order_artifact_roots(job):
        try:
            root_r = os.path.realpath(root)
        except OSError:
            continue
        if not (bam_abs == root_r or bam_abs.startswith(root_r + os.sep)):
            continue
        try:
            rel = os.path.relpath(bam_abs, root).replace("\\", "/")
        except ValueError:
            continue
        if ".." in rel:
            continue
        bai = bam_abs + ".bai"
        index_rel: Optional[str] = None
        if os.path.isfile(bai):
            try:
                index_rel = os.path.relpath(bai, root).replace("\\", "/")
            except ValueError:
                index_rel = None
        return {"rel_path": rel, "label": os.path.basename(bam_abs),
                "has_index": bool(index_rel), "index_rel_path": index_rel}
    return None


def _list_order_bam_tracks(job: Job, cap: int = 32) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen_real: set = set()
    bam_search_roots: List[str] = []
    for p in prior_reuse_artifact_roots(job):
        if p and os.path.isdir(p):
            bam_search_roots.append(p)
    for attr in ("analysis_dir", "output_dir"):
        p = (getattr(job, attr, None) or "").strip()
        if p and os.path.isdir(p):
            bam_search_roots.append(p)
    for r in _order_artifact_roots(job):
        if r and os.path.isdir(r) and r not in bam_search_roots:
            bam_search_roots.append(r)

    for root in bam_search_roots:
        if len(out) >= cap:
            break
        if not root or not os.path.isdir(root):
            continue
        try:
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    if not fn.lower().endswith(".bam"):
                        continue
                    fp = os.path.join(dirpath, fn)
                    if not os.path.isfile(fp):
                        continue
                    try:
                        rp = os.path.realpath(fp)
                    except OSError:
                        continue
                    if rp in seen_real:
                        continue
                    seen_real.add(rp)
                    track = _order_file_rel_from_abs_bam(job, fp)
                    if track:
                        track = _annotate_bam_track_ancillary(job, track)
                        out.append(track)
                    if len(out) >= cap:
                        break
                if len(out) >= cap:
                    break
        except OSError:
            pass
    return out


# ── Gene knowledge helpers ─────────────────────────────────────

def _load_result_dict_for_gene_knowledge(order_id: str, job) -> Optional[Dict[str, Any]]:
    result_json_path = None
    if getattr(job, "service_code", None) in _CARRIER_LIKE:
        from .services.carrier_screening.plugin import carrier_result_json_path
        result_json_path = carrier_result_json_path(job)
    elif job.output_dir:
        result_json_path = os.path.join(job.output_dir, "result.json")
    if result_json_path and os.path.isfile(result_json_path):
        with open(result_json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    store = get_queue_manager().store
    if store:
        return store.get_result_json(order_id)
    return None


def _compute_order_gene_knowledge(
    order_id: str, job,
    enrich: bool, gene_filter: Optional[str] = None,
    force_refresh: bool = False, genes_csv: Optional[str] = None,
) -> Dict[str, Any]:
    from .services.carrier_screening.gene_knowledge_db import (
        ensure_gene_knowledge_full_text, init_gene_knowledge_database,
        load_variant_knowledge_for_keys, make_variant_key,
        read_gene_knowledge_full_row, refresh_gene_knowledge_from_gemini,
    )
    db_path = (settings.gene_knowledge_db or "").strip()
    if not db_path:
        msg = "gene_knowledge_db is not configured"
        return {"gene_knowledge_db_configured": False, "genes": {}, "variants": {}, "message": msg, "error": msg}

    init_gene_knowledge_database(db_path)
    result_data = _load_result_dict_for_gene_knowledge(order_id, job)
    if not isinstance(result_data, dict):
        msg = "result.json not available for this order"
        return {"gene_knowledge_db_configured": True, "genes": {}, "variants": {}, "message": msg, "error": msg}

    variants = result_data.get("variants") or []
    order_genes = {(v.get("gene") or "").strip().upper() for v in variants if (v.get("gene") or "").strip()}
    genes = sorted(order_genes)
    gf = (gene_filter or "").strip().upper() or None
    if gf:
        if gf not in order_genes:
            return {"gene_knowledge_db_configured": True, "genes": {}, "variants": {},
                    "error": f"Gene {gf} is not among this order's variants", "enrich_requested": enrich,
                    "force_refresh": force_refresh, "gemini_available": bool((settings.gemini_api_key or "").strip())}
        genes = [gf]
    elif genes_csv is not None:
        raw = (genes_csv or "").strip()
        if not raw:
            genes = []
        else:
            requested = {x.strip().upper() for x in raw.split(",") if x.strip()}
            genes = sorted(order_genes & requested)

    gemini_key = (settings.gemini_api_key or "").strip()
    model = getattr(settings, "gene_knowledge_gemini_model", "gemini-2.5-flash")
    allow_gemini = bool(enrich and gemini_key)

    gene_set = set(genes)
    variant_keys_set: set = set()
    for v in variants:
        g = (v.get("gene") or "").strip().upper()
        if g and g in gene_set:
            variant_keys_set.add(make_variant_key(g, str(v.get("hgvsc") or ""), str(v.get("hgvsp") or "")))
    variants_out = load_variant_knowledge_for_keys(db_path, list(variant_keys_set))

    if force_refresh and not gemini_key:
        return {"gene_knowledge_db_configured": True, "genes": {}, "variants": variants_out,
                "error": "GEMINI_API_KEY not configured", "enrich_requested": enrich,
                "force_refresh": True, "gemini_available": False}

    out: Dict[str, Any] = {}
    gemini_fetch_error: Optional[str] = None
    for gene in genes:
        if force_refresh and gemini_key:
            row, gerr = refresh_gene_knowledge_from_gemini(gene, db_path, gemini_key, model=model)
            if gerr:
                gemini_fetch_error = gerr
        elif allow_gemini:
            row = ensure_gene_knowledge_full_text(gene, db_path, gemini_key, model=model, allow_gemini=True)
        else:
            row = read_gene_knowledge_full_row(gene, db_path)
        out[gene] = row if row else {}

    ret: Dict[str, Any] = {
        "gene_knowledge_db_configured": True, "genes": out, "variants": variants_out,
        "enrich_requested": enrich, "force_refresh": force_refresh, "gemini_available": bool(gemini_key),
    }
    if force_refresh and gemini_fetch_error:
        ret["gemini_fetch_error"] = gemini_fetch_error
    return ret


def _put_order_gene_knowledge(order_id: str, job, body, genes_csv: Optional[str] = None) -> Dict[str, Any]:
    from .services.carrier_screening.gene_knowledge_db import (
        init_gene_knowledge_database, read_gene_knowledge_full_row, upsert_gene_data,
    )
    db_path = (settings.gene_knowledge_db or "").strip()
    if not db_path:
        raise HTTPException(status_code=503, detail="gene_knowledge_db is not configured")
    gene = (body.gene or "").strip().upper()
    if not gene:
        raise HTTPException(status_code=400, detail="gene is required")
    result_data = _load_result_dict_for_gene_knowledge(order_id, job)
    if not isinstance(result_data, dict):
        raise HTTPException(status_code=400, detail="result.json not available for this order")
    variants = result_data.get("variants") or []
    order_genes = {(v.get("gene") or "").strip().upper() for v in variants if (v.get("gene") or "").strip()}
    if gene not in order_genes:
        raise HTTPException(status_code=400, detail=f"Gene {gene} is not among this order's variants")
    if genes_csv is not None:
        raw = (genes_csv or "").strip()
        allowed = {x.strip().upper() for x in raw.split(",") if x.strip()} if raw else set()
        if gene not in allowed:
            raise HTTPException(status_code=400, detail="Gene not in current selection")
    init_gene_knowledge_database(db_path)
    upsert_gene_data(db_path, {
        "gene_symbol": gene, "function_summary": body.function_summary or "",
        "disease_association": body.disease_association or "", "disorder": body.disorder or "",
        "omim_number": body.omim_number or "", "inheritance": body.inheritance or "",
    })
    row = read_gene_knowledge_full_row(gene, db_path)
    return {"ok": True, "gene": gene, "row": row or {}}


def _put_order_variant_knowledge(order_id: str, job, body, genes_csv: Optional[str] = None) -> Dict[str, Any]:
    from .services.carrier_screening.gene_knowledge_db import (
        init_gene_knowledge_database, make_variant_key,
        read_variant_knowledge_row, upsert_variant_knowledge,
    )
    db_path = (settings.gene_knowledge_db or "").strip()
    if not db_path:
        raise HTTPException(status_code=503, detail="gene_knowledge_db is not configured")
    vk = (body.variant_key or "").strip()
    if not vk:
        raise HTTPException(status_code=400, detail="variant_key is required")
    result_data = _load_result_dict_for_gene_knowledge(order_id, job)
    if not isinstance(result_data, dict):
        raise HTTPException(status_code=400, detail="result.json not available for this order")
    variants = result_data.get("variants") or []
    matched: Optional[Dict[str, Any]] = None
    for v in variants:
        g = (v.get("gene") or "").strip().upper()
        if g and make_variant_key(g, str(v.get("hgvsc") or ""), str(v.get("hgvsp") or "")) == vk:
            matched = v
            break
    if not matched:
        raise HTTPException(status_code=400, detail="variant_key does not match any variant")
    gene = (matched.get("gene") or "").strip().upper()
    if genes_csv is not None:
        raw = (genes_csv or "").strip()
        allowed = {x.strip().upper() for x in raw.split(",") if x.strip()} if raw else set()
        if gene not in allowed:
            raise HTTPException(status_code=400, detail="Gene not in current selection")
    init_gene_knowledge_database(db_path)
    upsert_variant_knowledge(db_path, {
        "variant_key": vk, "gene_symbol": gene,
        "hgvsc": str(matched.get("hgvsc") or ""), "hgvsp": str(matched.get("hgvsp") or ""),
        "variant_notes": body.variant_notes or "",
    })
    row = read_variant_knowledge_row(vk, db_path)
    return {"ok": True, "variant_key": vk, "row": row or {}}


def _dashboard_order_updated_iso(j: Job) -> str:
    return j.updated_at or j.completed_at or j.started_at or j.created_at or ""


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
        "environment": settings.app_env,
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
    t = (dto_type or "").strip()
    if t in _NIPT_TYPES or t.upper() == "NIPT":
        return "nipt"
    if t in _SGNIPT_TYPES or t.upper() in {"SGNIPT", "SG_NIPT", "SG-NIPT"}:
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

        if service_code in ("sgnipt", "nipt"):
            # Both NIPT variants need the Portal FASTQ download dance
            # before enqueue. Run that asynchronously so the Platform
            # request returns quickly.
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


@app.post("/order/{order_id}/report/preview")
async def preview_report_html(order_id: str, request: ReportGenerateRequest):
    """
    Render the report Jinja template as HTML and return it (no PDF, no disk writes).
    Portal uses this for in-browser preview + manual editing before final generation.
    """
    queue_manager = get_queue_manager()
    job = queue_manager.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    plugin = get_plugin(job.service_code)
    if not plugin or not hasattr(plugin, "generate_report"):
        raise HTTPException(status_code=400, detail="Service does not support report generation")

    if job.service_code not in _CARRIER_LIKE:
        raise HTTPException(status_code=400, detail="Preview is only supported for carrier-like services")

    from .services.carrier_screening.report import (
        carrier_report_template_kind,
        report_languages_from_order,
        _carrier_order_flat,
        generate_report_json,
        _render_html_for_language,
        carrier_pdf_jinja_stem,
        CARRIER_PDF_SOLO_KINDS,
    )
    from .services.carrier_screening.plugin import (
        carrier_report_output_dir,
        resolve_carrier_pdf_template_dir,
        _extra_result_json_paths_for_carrier_report,
    )

    p_raw = job.params or {}
    kind = carrier_report_template_kind(p_raw)
    if kind is None:
        raise HTTPException(status_code=400, detail="PDF report is not supported for this order type.")
    langs_obj = report_languages_from_order(p_raw)
    if langs_obj is None:
        raise HTTPException(status_code=400, detail="Report language must be EN, CN, or KO.")
    languages = langs_obj if isinstance(langs_obj, list) else [langs_obj]

    output_dir = carrier_report_output_dir(job)
    template_dir = resolve_carrier_pdf_template_dir()
    params = _carrier_order_flat(p_raw)

    if kind in CARRIER_PDF_SOLO_KINDS:
        partner_info = None
    else:
        partner_info = request.partner_info

    pi = dict(request.patient_info) if request.patient_info else {}
    if not (pi.get("name") or "").strip():
        pi["name"] = (params.get("patient_name") or "").strip() or job.sample_name

    confirmed_variants = request.confirmed_variants or []
    report_json_path = os.path.join(output_dir, "report.json")

    def _render_preview():
        generate_report_json(
            order_id=job.order_id,
            sample_name=job.sample_name,
            confirmed_variants=confirmed_variants,
            reviewer_info=request.reviewer_info,
            qc_summary={},
            output_dir=output_dir,
            patient_info=pi,
            partner_info=partner_info,
            order_params=p_raw,
            report_language=(languages[0] if languages else "EN"),
            disease_gene_json=None,
            gene_knowledge_db=settings.gene_knowledge_db or None,
            gene_knowledge_enrich_on_report=False,
            gene_knowledge_gemini_on_report=False,
            gemini_api_key=None,
            gene_knowledge_gemini_model=None,
            extra_result_json_paths=_extra_result_json_paths_for_carrier_report(job),
            pdf_template_kind=kind,
        )

        with open(report_json_path, "r", encoding="utf-8") as f:
            report_data = json.load(f)

        from .services.carrier_screening.dark_genes import sanitize_dark_genes_payload_for_pdf_render
        from .services.carrier_screening.pgx_report import sanitize_pgx_payload_for_pdf_render
        from .services.carrier_screening.report import (
            _merge_dark_genes_from_result_json_for_pdf,
            _merge_pgx_from_result_json_for_pdf,
            _strip_supplemental_dark_genes_findings,
            apply_supplemental_dark_genes_findings_to_report_data,
            pdf_template_kind_excludes_dark_genes,
        )
        extras = _extra_result_json_paths_for_carrier_report(job)
        md_r = report_data.get("report_metadata") or {}
        _pdf_tk_r = md_r.get("pdf_template_kind")
        if not isinstance(_pdf_tk_r, str):
            _pdf_tk_r = None
        if not pdf_template_kind_excludes_dark_genes(_pdf_tk_r):
            _merge_dark_genes_from_result_json_for_pdf(
                report_data, report_json_path, extra_result_json_paths=extras
            )
            apply_supplemental_dark_genes_findings_to_report_data(report_data)
            sanitize_dark_genes_payload_for_pdf_render(report_data)
        else:
            report_data.pop("dark_genes", None)
            _strip_supplemental_dark_genes_findings(report_data)
        _merge_pgx_from_result_json_for_pdf(report_data, report_json_path, extra_result_json_paths=extras)
        sanitize_pgx_payload_for_pdf_render(report_data)

        is_couple = report_data.get("report_metadata", {}).get("is_couple", False)
        result = {}
        for lang in languages:
            html_content = _render_html_for_language(report_data, lang, template_dir, is_couple)
            pdf_kind = report_data.get("report_metadata", {}).get("pdf_template_kind")
            stem = carrier_pdf_jinja_stem(pdf_kind, is_couple)
            result[lang] = {
                "html": html_content,
                "template": f"{stem}_{lang.upper()}.html",
            }
        return result

    try:
        rendered = await asyncio.to_thread(_render_preview)
    except Exception as e:
        logger.error(f"Report preview failed for {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Preview rendering failed: {e}")

    return JSONResponse({
        "status": "ok",
        "order_id": order_id,
        "languages": rendered,
    })


@app.post("/order/{order_id}/report/from-html")
async def generate_report_from_html(order_id: str, request: Request):
    """
    Accept edited HTML per language, generate PDFs, mark order REPORT_READY,
    and trigger platform upload.

    Body: { "languages": { "EN": "<html>...</html>", "CN": "<html>..." } }
    """
    queue_manager = get_queue_manager()
    job = queue_manager.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    if job.service_code not in _CARRIER_LIKE:
        raise HTTPException(status_code=400, detail="Only carrier-like services supported")

    body = await request.json()
    languages_html: dict = body.get("languages") or {}
    if not languages_html:
        raise HTTPException(status_code=400, detail="languages dict is required")

    from .services.carrier_screening.plugin import (
        carrier_report_output_dir,
        resolve_carrier_pdf_template_dir,
    )

    output_dir = carrier_report_output_dir(job)
    template_dir = resolve_carrier_pdf_template_dir()
    os.makedirs(output_dir, exist_ok=True)

    raw_name = (job.params or {}).get("patient_name") or job.sample_name or "Patient"
    patient_name = "".join([c if c.isalnum() else "_" for c in raw_name]).replace("__", "_")

    generated_pdfs = []

    def _render_all():
        from weasyprint import HTML as WeasyprintHTML
        base_url = template_dir if template_dir else output_dir
        for lang, html_content in languages_html.items():
            lang_u = (lang or "EN").strip().upper()
            html_content = (html_content or "").strip()
            if not html_content:
                continue
            html_filename = f"Report_{order_id}_{patient_name}_{lang_u}.html"
            pdf_filename = f"Report_{order_id}_{patient_name}_{lang_u}.pdf"
            html_path = os.path.join(output_dir, html_filename)
            pdf_path = os.path.join(output_dir, pdf_filename)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            WeasyprintHTML(string=html_content, base_url=base_url).write_pdf(pdf_path)
            generated_pdfs.append(pdf_filename)
            logger.info(f"Generated PDF from edited HTML for {order_id}: {pdf_filename}")

    try:
        await asyncio.to_thread(_render_all)
    except ImportError:
        raise HTTPException(status_code=500, detail="weasyprint not installed")
    except Exception as e:
        logger.error(f"PDF from HTML failed for {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    if not generated_pdfs:
        raise HTTPException(status_code=400, detail="No PDFs were generated (empty HTML?)")

    await queue_manager.mark_report_ready(order_id, message="Report ready for download")

    store = queue_manager.store
    if store and output_dir:
        await asyncio.to_thread(ingest_report_json_from_disk, store, job, output_dir)

    report_files: list = []
    report_json_path = os.path.join(output_dir, "report.json")
    if os.path.exists(report_json_path):
        report_files.append(OutputFile(
            file_path=report_json_path, file_type="report_json",
            file_name="report.json", content_type="application/json",
        ))
    for pat, ftype, mime in [
        ("Report_*.pdf", "report_pdf", "application/pdf"),
        ("Report_*.html", "report_html", "text/html"),
    ]:
        for fp in glob.glob(os.path.join(output_dir, pat)):
            report_files.append(OutputFile(
                file_path=fp, file_type=ftype,
                file_name=os.path.basename(fp), content_type=mime,
            ))

    platform_client = get_platform_client()
    if report_files and settings.platform_api_enabled:
        async def _upload_bg():
            try:
                results = await platform_client.upload_all_outputs(
                    order_id, job.service_code, report_files
                )
                ok = sum(1 for r in results.values() if r.status.value == "SUCCESS")
                logger.info(f"Platform upload for {order_id}: {ok}/{len(report_files)}")
            except Exception as exc:
                logger.exception(f"Platform upload failed for {order_id}: {exc}")
        asyncio.create_task(_upload_bg())

    return JSONResponse({
        "status": "ok",
        "order_id": order_id,
        "report_files": generated_pdfs,
        "message": f"Report generated: {len(generated_pdfs)} PDF(s). Order marked REPORT_READY.",
    })


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


@app.get("/debug/tracker")
async def get_tracker_status():
    """nipt-daemon compatible tracker status."""
    runner = get_runner()
    qm = get_queue_manager()
    return {
        "running_analyses": list(qm._running_jobs.keys()),
        "monitoring_tasks": {},
        "semaphore_callbacks": {},
        "queue_manager_running": list(qm._running_jobs.keys()),
        "running_pids": {oid: pid for oid, pid in getattr(runner, "_running_pids", {}).items()},
    }


# ══════════════════════════════════════════════════════════════
# ORDER UPDATE / FASTQ PATCH
# ══════════════════════════════════════════════════════════════

@app.patch("/order/{order_id}", response_model=OrderUpdateResponse)
async def update_order(order_id: str, request: OrderUpdateRequest):
    """
    SAVED / FAILED / CANCELLED / COMPLETED / REPORT_READY 주문 필드 수정.
    """
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    if job.status not in (
        OrderStatus.SAVED, OrderStatus.FAILED, OrderStatus.CANCELLED,
        OrderStatus.COMPLETED, OrderStatus.REPORT_READY,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Order {order_id} is {job.status.value}; only SAVED/FAILED/CANCELLED/COMPLETED/REPORT_READY can be edited",
        )
    submit = _order_merge_submit(job, request)
    plugin = get_plugin(submit.service_code)
    if not plugin:
        raise HTTPException(status_code=400, detail=f"Unknown service_code: {submit.service_code}")
    is_valid, error_msg = plugin.validate_params(submit.params or {})
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Invalid params: {error_msg}")
    new_job = _build_job_from_submit_request(submit.service_code, submit)
    new_job.status = job.status
    new_job.created_at = job.created_at
    new_job.updated_at = now_kst_iso()
    if job.status in (OrderStatus.FAILED, OrderStatus.CANCELLED):
        new_job.progress = 0
        new_job.message = ""
        new_job.error_log = None
        new_job.completed_at = None
        new_job.started_at = None
    elif job.status == OrderStatus.SAVED:
        new_job.progress = 0
        new_job.message = ""
    elif job.status in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
        new_job.progress = job.progress
        new_job.message = job.message
        new_job.completed_at = job.completed_at
        new_job.started_at = job.started_at
        new_job.error_log = job.error_log
        new_job.pid = job.pid
        new_job.exit_code = job.exit_code
        new_job.duration = job.duration
    try:
        await qm.replace_edited_job(new_job, previous_order_id=order_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return OrderUpdateResponse(status="updated", order_id=new_job.order_id, message="Order updated")


@app.patch("/order/{order_id}/fastq", response_model=None)
async def patch_order_fastq_paths(order_id: str, request: UpdateFastqPathsRequest):
    """Update FASTQ paths for a queued/saved order."""
    if request.fastq_r1_path is None and request.fastq_r2_path is None:
        raise HTTPException(status_code=400, detail="Provide fastq_r1_path and/or fastq_r2_path")
    new_r1: Optional[str] = None
    new_r2: Optional[str] = None
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    svc = job.service_code
    if request.fastq_r1_path is not None:
        s = request.fastq_r1_path.strip()
        new_r1 = "" if not s else _validate_optional_fastq_file(s, svc)
    if request.fastq_r2_path is not None:
        s = request.fastq_r2_path.strip()
        new_r2 = "" if not s else _validate_optional_fastq_file(s, svc)
    ok, err = await qm.update_queued_job_fastq_paths(order_id, fastq_r1_path=new_r1, fastq_r2_path=new_r2)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    job = qm.get_job(order_id)
    return {
        "status": "ok", "order_id": order_id,
        "fastq_r1_path": job.fastq_r1_path if job else None,
        "fastq_r2_path": job.fastq_r2_path if job else None,
    }


@app.post("/order/{order_id}/cancel")
async def cancel_order(order_id: str):
    """Cancel a queued or running order."""
    qm = get_queue_manager()
    runner = get_runner()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    if job.status == OrderStatus.QUEUED:
        ok = await qm.request_cancel_queued(order_id)
        return {"status": "cancel_requested" if ok else "not_queued", "order_id": order_id}
    if job.status in (OrderStatus.RUNNING, OrderStatus.PROCESSING):
        killed = await runner.cancel_job(order_id)
        return {"status": "stop_requested", "order_id": order_id, "pid_killed": killed}
    return {"status": "not_active", "order_id": order_id, "current_status": job.status.value}


@app.post("/order/{order_id}/purge-db")
async def purge_order_database_record(order_id: str, force: bool = Query(False)):
    """Remove order record from DB / memory without deleting disk artifacts."""
    qm = get_queue_manager()
    ok, message, detail = await qm.purge_order_db_only(order_id, force=force)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "ok", "order_id": order_id, "message": message, **detail}


# ══════════════════════════════════════════════════════════════
# REVIEW PATCH ALIASES (PATCH = same as POST; for proxies that block PATCH)
# ══════════════════════════════════════════════════════════════

@app.patch("/order/{order_id}/dark-genes-review")
async def patch_dark_genes_review(order_id: str, body: DarkGenesReviewRequest):
    return await _dark_genes_review_impl(order_id, body)


@app.patch("/order/{order_id}/pgx-review")
async def patch_pgx_review(order_id: str, body: PgxReviewRequest):
    return await _pgx_review_impl(order_id, body)


# ══════════════════════════════════════════════════════════════
# COVERAGE / GENE-KNOWLEDGE / FILE APIs
# ══════════════════════════════════════════════════════════════

@app.get("/order/{order_id}/gene-coverage/{gene_symbol}")
async def get_order_gene_coverage(order_id: str, gene_symbol: str):
    """Twist exome target intervals for a gene — supports Variant Review."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    if job.service_code not in _CARRIER_LIKE:
        raise HTTPException(status_code=400, detail="Gene coverage is only available for carrier-like services.")
    from .services.gene_panel_coverage import build_gene_panel_coverage_report
    try:
        report = build_gene_panel_coverage_report(job, gene_symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(content=report)


@app.get("/order/{order_id}/coverage-context")
async def get_order_coverage_context(order_id: str):
    """Coverage tab: interpretation genes and BAM paths for IGV.js."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    _coverage_ok = _CARRIER_LIKE | {"sgnipt"}
    if job.service_code not in _coverage_ok:
        raise HTTPException(status_code=400, detail="Coverage context is only available for carrier-like and sgnipt services.")
    if job.service_code == "sgnipt":
        genes: list = []
    else:
        from .services.wes_panels import interpretation_gene_set_for_job
        genes = sorted(interpretation_gene_set_for_job(job))
    bams_all = _list_order_bam_tracks(job)
    bams_igv = [t for t in bams_all if not t.get("ancillary")]
    ctx: Dict[str, Any] = {
        "order_id": order_id, "interpretation_genes": genes,
        "bam_tracks": bams_igv, "genome_id": "hg38",
    }
    if bams_all and not bams_igv:
        ctx["igv_bam_message"] = "Only ancillary BAMs found; they are not used for auto-IGV."
    if (job.params or {}).get("_prior_reuse"):
        pid = (job.params or {}).get("_prior_reuse_order_id")
        if isinstance(pid, str) and pid.strip():
            ctx["prior_reuse_order_id"] = pid.strip()
    return JSONResponse(content=ctx, headers=_RESULT_JSON_CACHE_HEADERS)


@app.get("/order/{order_id}/gene-knowledge")
async def get_order_gene_knowledge(
    order_id: str,
    enrich: bool = Query(False, description="If true, call Gemini for missing genes"),
    gene: Optional[str] = Query(None, description="Single gene symbol filter"),
    force: bool = Query(False, description="Always re-run Gemini for the selected gene(s)"),
    genes: Optional[str] = Query(None, description="Comma-separated gene symbols to include"),
):
    """Gene-level text from the gene_knowledge SQLite cache."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    return await asyncio.to_thread(_compute_order_gene_knowledge, order_id, job, enrich, gene, force, genes)


@app.put("/order/{order_id}/gene-knowledge")
async def put_order_gene_knowledge(
    order_id: str,
    body: GeneKnowledgeSaveRequest,
    genes: Optional[str] = Query(None, description="Comma-separated gene symbols allowed"),
):
    """Save portal edits to the shared gene_knowledge SQLite cache."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    return await asyncio.to_thread(_put_order_gene_knowledge, order_id, job, body, genes)


@app.put("/order/{order_id}/variant-knowledge")
async def put_order_variant_knowledge(
    order_id: str,
    body: VariantKnowledgeSaveRequest,
    genes: Optional[str] = Query(None),
):
    """Save per-variant notes to variant_knowledge."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    return await asyncio.to_thread(_put_order_variant_knowledge, order_id, job, body, genes)


@app.get("/order/{order_id}/files")
async def list_order_files(order_id: str):
    """List output files for an order."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    roots = _order_artifact_roots(job)
    if not roots:
        return JSONResponse(content={"files": [], "total": 0}, headers=_RESULT_JSON_CACHE_HEADERS)
    best: Dict[str, Tuple[str, int, int]] = {}
    for output_dir in roots:
        try:
            names = os.listdir(output_dir)
        except OSError:
            continue
        for fname in names:
            fpath = os.path.join(output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                mtime_ms = int(round(os.path.getmtime(fpath) * 1000))
            except OSError:
                mtime_ms = 0
            try:
                sz = os.path.getsize(fpath)
            except OSError:
                sz = 0
            prev = best.get(fname)
            if prev is None or mtime_ms > prev[1]:
                best[fname] = (fpath, mtime_ms, sz)
    files: List[Dict[str, Any]] = []
    for fname in sorted(best.keys()):
        fpath, mtime_ms, sz = best[fname]
        files.append({"name": fname, "size": sz, "mtime_ms": mtime_ms, "type": _guess_file_type(fname)})
    return JSONResponse(content={"files": files, "total": len(files)}, headers=_RESULT_JSON_CACHE_HEADERS)


@app.head("/order/{order_id}/file/{filename:path}")
async def head_order_file(order_id: str, filename: str):
    """HEAD for IGV.js / byte-range clients."""
    file_path = _resolve_order_file_or_404(order_id, filename)
    try:
        stat = os.stat(file_path)
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")
    return PlainTextResponse(
        content="",
        headers={
            "Content-Length": str(stat.st_size),
            "Content-Type": _guess_content_type(filename),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/order/{order_id}/file/{filename:path}")
async def download_order_file(order_id: str, filename: str):
    """Download a specific output file."""
    file_path = _resolve_order_file_or_404(order_id, filename)
    base_name = os.path.basename(filename) or "download"
    return FileResponse(
        path=file_path, filename=base_name,
        media_type=_guess_content_type(filename),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/order/{order_id}/pipeline-log")
async def get_order_pipeline_log(
    order_id: str,
    max_bytes: int = Query(default=524_288, ge=4096, le=4_194_304, description="Max bytes from end of log"),
):
    """Return pipeline.log content as plain text."""
    qm = get_queue_manager()
    job = qm.get_job(order_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")
    log_dir = job.log_dir
    if not log_dir:
        raise HTTPException(status_code=404, detail="log_dir not set for this order")
    log_dir_real = os.path.realpath(log_dir)
    file_path = os.path.realpath(os.path.join(log_dir, "pipeline.log"))
    if not file_path.startswith(log_dir_real + os.sep):
        raise HTTPException(status_code=400, detail="invalid log path")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="pipeline.log not found (pipeline may not have started yet)")
    try:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
                raw = f.read()
            else:
                raw = f.read()
        text = raw.decode("utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"cannot read pipeline.log: {e}")
    return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")


# ══════════════════════════════════════════════════════════════
# FASTQ / BAM-CSV BROWSE
# ══════════════════════════════════════════════════════════════

@app.get("/api/fastq/browse")
async def browse_fastq_short_path(
    path: str = Query(default="", description="Relative path under FASTQ root"),
    service_code: Optional[str] = Query(default=None, description="sgnipt | carrier_screening"),
):
    """FASTQ directory browser."""
    return _browse_fastq_directory_payload(path, service_code)


@app.get("/api/portal/browse/fastq")
async def browse_fastq_directory(
    path: str = Query(default=""),
    service_code: Optional[str] = Query(default=None),
):
    """Same as /api/fastq/browse (backward compat)."""
    return _browse_fastq_directory_payload(path, service_code)


@app.get("/api/portal/bam-csv/browse")
async def browse_bam_csv_portal(
    path: str = Query(default=""),
    service_code: Optional[str] = Query(default=None),
    abs_path: Optional[str] = Query(default=None),
):
    """BAM samplesheet CSV browser."""
    return _browse_bam_csv_directory_payload(path, service_code, abs_path)


@app.get("/api/bam-csv/browse")
async def browse_bam_csv(
    path: str = Query(default=""),
    service_code: Optional[str] = Query(default=None),
    abs_path: Optional[str] = Query(default=None),
):
    """Same as /api/portal/bam-csv/browse."""
    return _browse_bam_csv_directory_payload(path, service_code, abs_path)


@app.get("/api/portal/bam-csv/sample-ids")
async def read_bam_csv_sample_ids(abs_path: str = Query(description="Absolute path to BAM samplesheet CSV")):
    """Read sample_id column from a CSV samplesheet."""
    path_clean = abs_path.strip()
    if not os.path.isfile(path_clean):
        raise HTTPException(status_code=404, detail=f"File not found: {path_clean}")
    sample_ids: List[str] = []
    try:
        with open(path_clean, newline="", encoding="utf-8-sig") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                sid = (row.get("sample_id") or "").strip()
                if sid:
                    sample_ids.append(sid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot parse CSV: {e}") from e
    return {"abs_path": path_clean, "sample_ids": sample_ids}


# ══════════════════════════════════════════════════════════════
# WES PANELS
# ══════════════════════════════════════════════════════════════

@app.get("/api/portal/wes-panels")
async def portal_wes_panels():
    """Orderable WES/exome interpretation panels."""
    from .services.wes_panels import load_wes_panels_raw, panels_for_api_response
    raw = load_wes_panels_raw()
    ver = raw.get("version", 1) if isinstance(raw, dict) else 1
    return {"version": ver, "panels": panels_for_api_response()}


@app.post("/api/portal/wes-panels/custom")
async def portal_wes_panels_custom_save(body: WesPanelCustomSave):
    """Create or update a user-defined panel package."""
    from .services.wes_panels import save_custom_panel
    try:
        extra = save_custom_panel(
            body.id, body.label, body.category, body.description,
            body.backbone_bed, body.disease_bed, body.genes, body.genes_text,
            body.gene_source_bed, body.skip_generated_bed,
        )
        return {"status": "ok", **extra}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"Could not write panel catalog: {e}") from e


@app.delete("/api/portal/wes-panels/custom/{panel_id:path}")
async def portal_wes_panels_custom_delete(panel_id: str):
    """Remove a custom panel."""
    from .services.wes_panels import delete_custom_panel
    pid = (panel_id or "").strip().strip("/")
    if not pid:
        raise HTTPException(status_code=400, detail="panel_id required")
    try:
        ok = delete_custom_panel(pid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="Custom panel not found")
    return {"status": "ok", "deleted": pid}


@app.get("/api/wes-panels")
async def api_wes_panels_alias():
    """Same as /api/portal/wes-panels."""
    return await portal_wes_panels()


# ══════════════════════════════════════════════════════════════
# QUEUE DASHBOARD BUCKET
# ══════════════════════════════════════════════════════════════

@app.get("/queue/dashboard-bucket")
async def get_dashboard_bucket(
    bucket: str = Query(..., description="queued | running | completed | failed"),
    service_code: Optional[str] = Query(default=None),
    sort: str = Query(default="order_updated"),
    order: str = Query(default="desc"),
):
    """Dashboard stats card: list of orders by bucket."""
    allowed_bucket = {"queued", "running", "completed", "failed"}
    b = (bucket or "").strip().lower()
    if b not in allowed_bucket:
        raise HTTPException(status_code=400, detail=f"Invalid bucket: {bucket}")
    order_l = (order or "desc").strip().lower()
    if order_l not in ("asc", "desc"):
        order_l = "desc"
    sk = (sort or "order_updated").strip().lower()
    if sk not in ("order_id", "status", "order_updated", "message"):
        sk = "order_updated"
    qm = get_queue_manager()
    jobs = qm.get_dashboard_bucket_jobs(b, service_code)
    rev = order_l == "desc"
    if sk == "order_id":
        jobs = sorted(jobs, key=lambda j: (j.order_id or "").lower(), reverse=rev)
    elif sk == "status":
        jobs = sorted(jobs, key=lambda j: j.status.value if j.status else "", reverse=rev)
    elif sk == "message":
        jobs = sorted(jobs, key=lambda j: (j.message or "").lower(), reverse=rev)
    else:
        jobs = sorted(jobs, key=_dashboard_order_updated_iso, reverse=rev)
    return {
        "bucket": b, "service_code": service_code, "sort": sk, "order": order_l,
        "total": len(jobs),
        "orders": [
            {"order_id": j.order_id, "status": j.status.value,
             "order_updated": _dashboard_order_updated_iso(j), "message": j.message or ""}
            for j in jobs
        ],
    }


# ══════════════════════════════════════════════════════════════
# LITERATURE API
# ══════════════════════════════════════════════════════════════

@app.get("/api/literature/search")
async def literature_search(
    gene: str = Query(..., description="Gene symbol"),
    hgvsc: str = Query(""), hgvsp: str = Query(""), effect: str = Query(""),
    force_refresh: bool = Query(False), max_results: int = Query(10, ge=1, le=30),
):
    """Search PubMed for variant literature."""
    if not getattr(settings, "literature_enabled", False):
        raise HTTPException(status_code=503, detail="Literature search is disabled (LITERATURE_ENABLED=false)")
    from .services.carrier_screening.literature import search_variant_literature
    result = await search_variant_literature(
        gene=gene, hgvsc=hgvsc, hgvsp=hgvsp, effect=effect,
        max_results=max_results,
        ncbi_email=getattr(settings, "ncbi_email", ""),
        ncbi_api_key=getattr(settings, "ncbi_api_key", None),
        ncbi_tool=getattr(settings, "ncbi_tool", "gx-daemon"),
        force_refresh=force_refresh,
    )
    return result


@app.get("/api/literature/articles")
def literature_list_articles(
    cursor: int = Query(0, ge=0), count: int = Query(50, ge=1, le=200),
    search: str = Query(""), sort_by: str = Query("cached_at"),
):
    """List cached PubMed articles."""
    if not getattr(settings, "literature_enabled", False):
        raise HTTPException(status_code=503, detail="Literature search is disabled")
    from .services.carrier_screening.literature import list_cached_articles
    return list_cached_articles(cursor=cursor, count=count, search=search, sort_by=sort_by)


@app.get("/api/literature/articles/stats")
def literature_stats():
    """Literature cache statistics."""
    if not getattr(settings, "literature_enabled", False):
        return {"enabled": False}
    from .services.carrier_screening.literature import get_literature_stats
    stats = get_literature_stats()
    stats["enabled"] = True
    return stats


@app.get("/api/literature/articles/{pmid}")
def literature_get_article(pmid: str):
    """Get a cached article by PMID."""
    from .services.carrier_screening.literature import get_cached_article
    art = get_cached_article(pmid)
    if not art:
        raise HTTPException(status_code=404, detail=f"Article {pmid} not found in cache")
    return art


@app.delete("/api/literature/articles/{pmid}")
def literature_delete_article(pmid: str):
    """Delete one article from cache."""
    from .services.carrier_screening.literature import delete_cached_article
    deleted = delete_cached_article(pmid)
    return {"deleted": deleted, "pmid": pmid}


@app.delete("/api/literature/cache")
def literature_clear_cache():
    """Clear entire literature cache."""
    from .services.carrier_screening.literature import clear_literature_cache
    return clear_literature_cache()


@app.get("/api/literature/searches")
def literature_list_searches(
    gene: str = Query(""), cursor: int = Query(0, ge=0), count: int = Query(50, ge=1, le=200),
):
    """List cached variant searches."""
    from .services.carrier_screening.literature import list_cached_searches
    return list_cached_searches(gene=gene, cursor=cursor, count=count)


# ══════════════════════════════════════════════════════════════
# TEST / MOCK (Dev only)
# ══════════════════════════════════════════════════════════════

@app.post("/test/inject-mock-job")
async def inject_mock_job(
    order_id: str = Body(default="CS-TEST-001"),
    sample_name: str = Body(default="SAMPLE_001"),
    output_dir: str = Body(default="/tmp/carrier-screening/output/test/SAMPLE_001"),
    status: str = Body(default="COMPLETED"),
):
    """[DEV ONLY] Inject a mock job into QueueManager for testing."""
    qm = get_queue_manager()
    existing = qm.get_job(order_id)
    if existing:
        return {"status": "exists", "order_id": order_id, "message": "Job already exists"}
    job = Job(
        order_id=order_id, service_code="carrier_screening", sample_name=sample_name,
        work_dir="test", fastq_r1_path="/tmp/test_R1.fastq.gz", fastq_r2_path="/tmp/test_R2.fastq.gz",
        params={}, fastq_dir="/tmp/carrier-screening/fastq/test/" + sample_name,
        analysis_dir="/tmp/carrier-screening/analysis/test/" + sample_name,
        output_dir=output_dir, log_dir="/tmp/carrier-screening/log/test/" + sample_name,
    )
    status_enum = OrderStatus(status) if status in [s.value for s in OrderStatus] else OrderStatus.COMPLETED
    job.status = status_enum
    job.created_at = now_kst_iso()
    job.progress = 100 if status_enum == OrderStatus.COMPLETED else 0
    if status_enum == OrderStatus.COMPLETED:
        job.completed_at = now_kst_iso()
        job.started_at = now_kst_iso()
        job.message = "Mock job - analysis complete"
        qm._completed_jobs[order_id] = job
    elif status_enum == OrderStatus.RUNNING:
        job.started_at = now_kst_iso()
        job.message = "Mock job - running"
        qm._running_jobs[order_id] = job
    else:
        job.message = "Mock job - queued"
        qm._jobs[order_id] = job
    logger.info(f"[TEST] Injected mock job: {order_id} ({status})")
    return {
        "status": "injected", "order_id": order_id, "sample_name": sample_name,
        "job_status": job.status.value, "output_dir": output_dir,
    }
