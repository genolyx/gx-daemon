"""
sgNIPT report generation — JSON + WeasyPrint PDF.

Data flow:
  result.json  +  confirmed_variants (from portal reviewer)
      ↓
  generate_sgnipt_report_json()  →  report.json
      ↓
  generate_sgnipt_report_pdf()   →  report_<LANG>.pdf
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SGNIPT_PDF_TEMPLATE_STEM = "sgnipt"
SGNIPT_SUPPORTED_LANGUAGES = ["EN", "KO"]

_ORIGIN_DISPLAY = {
    "fetal_specific": "Fetal",
    "fetal": "Fetal",
    "maternal_het": "Maternal",
    "maternal_hom": "Maternal",
    "maternal": "Maternal",
    "ambiguous": "Ambiguous",
    "unknown": "Unknown",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fmt_date(dt: Optional[datetime] = None) -> str:
    d = dt or datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%d")


def _pct(v: Optional[float]) -> Optional[float]:
    """Return float as-is (template multiplies by 100 and rounds)."""
    return v


def _extract_gene_from_target_name(target_name: str) -> str:
    """Parse first GENE_ prefix from long target_name string."""
    if not target_name:
        return ""
    part = target_name.split("|")[0].strip()
    if "_" in part:
        return part.split("_")[0].upper()
    return part.upper()


def _build_fetal_info(result_data: Dict[str, Any]) -> Dict[str, Any]:
    ff_detail = result_data.get("fetal_fraction_detail") or {}
    # Try sample-level fallback
    samples = result_data.get("samples") or []
    s0_ff = ((samples[0].get("fetal_fraction") or {}) if samples else {})

    primary_ff = ff_detail.get("primary_fetal_fraction") or ff_detail.get("primary_ff") or s0_ff.get("primary_ff")
    status = (ff_detail.get("status") or s0_ff.get("status") or "").upper()
    method = ff_detail.get("primary_method") or s0_ff.get("primary_method") or ""
    num_snps = ff_detail.get("num_informative_snps") or s0_ff.get("num_informative_snps")
    gender = (
        ff_detail.get("fetal_gender")
        or s0_ff.get("fetal_gender")
        or ff_detail.get("sex")
        or s0_ff.get("sex")
        or "Unknown"
    )
    # Normalise method string
    method_display = {
        "snp_informative": "SNP Informative",
        "snp": "SNP",
        "size_based": "Size-based",
        "y_chr": "Y Chromosome",
    }.get(method, method or "—")

    return {
        "fraction": primary_ff,
        "fraction_status": status if status else ("PASS" if primary_ff is not None else "FAILED"),
        "method": method_display,
        "num_informative_snps": num_snps,
        "gender": gender,
        "ci_lower": (ff_detail.get("confidence_interval") or {}).get("lower"),
        "ci_upper": (ff_detail.get("confidence_interval") or {}).get("upper"),
    }


def _build_upd_data(result_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    samples = result_data.get("samples") or []
    upd_raw = None
    for s in samples:
        if s.get("upd_analysis"):
            upd_raw = s["upd_analysis"]
            break
    if not upd_raw:
        return None

    per_chrom_raw = upd_raw.get("per_chrom_upd") or []
    per_chrom = []
    for c in per_chrom_raw:
        per_chrom.append({
            "chrom": c.get("chrom", ""),
            "call": c.get("call", "INSUFFICIENT_INFORMATION"),
            "n_mat_het": c.get("n_mat_het"),
            "n_mat_hom": c.get("n_mat_hom"),
            "clinical_note": c.get("clinical_note") or {},
            "notes": c.get("notes") or [],
        })

    return {
        "summary_call": upd_raw.get("summary_call", "INSUFFICIENT_INFORMATION"),
        "fetal_fraction": upd_raw.get("fetal_fraction"),
        "per_chrom": per_chrom,
        "upd_chroms": upd_raw.get("upd_chroms") or [],
    }


def _build_qc(result_data: Dict[str, Any]) -> Dict[str, Any]:
    samples = result_data.get("samples") or []
    s0 = samples[0] if samples else {}
    return {
        "bam_qc": s0.get("bam_qc") or {},
        "fastq_qc": s0.get("fastq_qc") or {},
    }


def _enrich_confirmed_variant(
    cv: Dict[str, Any],
    result_data: Dict[str, Any],
    db_path: Optional[str],
    gemini_key: Optional[str],
    model: str,
) -> Dict[str, Any]:
    """
    Merge gene knowledge (gene_description, disease_association, inheritance) into
    a confirmed variant dict. Falls back gracefully if DB/Gemini not configured.
    """
    v = dict(cv)

    # Normalise origin display
    raw_origin = (v.get("origin") or "").lower()
    v["origin"] = _ORIGIN_DISPLAY.get(raw_origin, v.get("origin") or "")

    # Try to pull gene knowledge from DB
    gene = (v.get("gene") or "").strip().upper()
    if gene and db_path:
        try:
            from .carrier_screening.gene_knowledge_db import (
                ensure_gene_knowledge_full_text,
                init_gene_knowledge_database,
            )
            init_gene_knowledge_database(db_path)
            row = ensure_gene_knowledge_full_text(
                gene, db_path, gemini_key or "", model=model,
                allow_gemini=bool(gemini_key),
            )
            if row:
                v.setdefault("function_summary", row.get("function_summary") or "")
                v.setdefault("disease_association", row.get("disease_association") or "")
                v.setdefault("disorder", v.get("disorder") or row.get("disorder") or "")
                v.setdefault("inheritance", row.get("inheritance") or "")
                v.setdefault("omim_number", row.get("omim_number") or "")
        except Exception as e:
            logger.debug("[sgnipt_report] gene_knowledge enrichment failed for %s: %s", gene, e)

    # Resolve disease from result.json clinical_findings if not set
    if not v.get("disease") and not v.get("disorder"):
        chrom = v.get("chrom", "")
        pos = v.get("pos")
        for cf in (result_data.get("clinical_findings") or []):
            if cf.get("chrom") == chrom and cf.get("pos") == pos:
                v["disease"] = cf.get("disease") or ""
                break

    return v


def _extract_genes_evaluated(result_data: Dict[str, Any]) -> List[str]:
    """Best-effort extraction of gene symbols from result.json."""
    genes: set = set()
    for v in (result_data.get("clinical_findings") or []) + (result_data.get("all_target_variants") or []):
        gene = (v.get("gene") or "").strip().upper()
        if not gene:
            tn = v.get("target_name") or ""
            gene = _extract_gene_from_target_name(tn)
        if gene:
            genes.add(gene)
    # Check gene_coverage_validation keys
    gcv = result_data.get("gene_coverage_validation") or {}
    for g in gcv.keys():
        if g:
            genes.add(g.strip().upper())
    return sorted(genes)


# --------------------------------------------------------------------------- #
# Public: generate_sgnipt_report_json
# --------------------------------------------------------------------------- #

def generate_sgnipt_report_json(
    order_id: str,
    sample_name: str,
    result_json_path: str,
    confirmed_variants: List[Dict[str, Any]],
    output_dir: str,
    *,
    reviewer_info: Optional[Dict[str, Any]] = None,
    patient_info: Optional[Dict[str, Any]] = None,
    report_language: str = "EN",
    gene_knowledge_db: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
    gemini_model: str = "gemini-2.5-flash",
) -> str:
    """
    Build report.json from result.json + reviewer-confirmed variants.
    Returns the path to the written report.json.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        with open(result_json_path, encoding="utf-8") as f:
            result_data: Dict[str, Any] = json.load(f)
    except Exception as e:
        raise RuntimeError(f"[sgnipt_report] Cannot read result.json at {result_json_path}: {e}") from e

    pi = patient_info or {}
    ri = reviewer_info or {}
    now_str = _fmt_date()

    # Enrich confirmed variants
    enriched_variants = [
        _enrich_confirmed_variant(cv, result_data, gene_knowledge_db, gemini_api_key, gemini_model)
        for cv in (confirmed_variants or [])
    ]

    overall_status = result_data.get("sgnipt_status") or "NO_CALL"
    # If reviewer confirmed any pathogenic variant, upgrade to POSITIVE
    for cv in enriched_variants:
        cls = (cv.get("classification") or "").lower()
        if "pathogenic" in cls:
            overall_status = "POSITIVE"
            break

    report_data = {
        "report_metadata": {
            "order_id": order_id,
            "report_date": now_str,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hospital": pi.get("hospital") or ri.get("hospital") or "",
            "doctor": pi.get("doctor") or ri.get("physician") or ri.get("name") or "",
            "reviewer": ri.get("name") or ri.get("reviewer") or "",
            "reviewer_credentials": ri.get("credentials") or "",
            "language": report_language.upper(),
            "service_code": "sgnipt",
        },
        "patient": {
            "name": pi.get("name") or sample_name or "",
            "sample_id": result_data.get("sample_id") or sample_name or "",
            "dob": pi.get("dob") or "",
            "collection_date": pi.get("collection_date") or "",
            "gestational_age": pi.get("gestational_age") or "",
        },
        "panel": result_data.get("panel") or "",
        "overall_status": overall_status,
        "status_flags": result_data.get("sgnipt_status_flags") or [],
        "fetal_info": _build_fetal_info(result_data),
        "confirmed_variants": enriched_variants,
        "upd": _build_upd_data(result_data),
        "qc": _build_qc(result_data),
        "genes_evaluated": _extract_genes_evaluated(result_data),
        "variant_analysis_summary": result_data.get("variant_analysis_summary") or {},
        # dark_genes populated later from result.json sibling if present
        "dark_genes": None,
    }

    # Attempt to pull dark_genes from result.json
    dg = result_data.get("dark_genes")
    if dg:
        report_data["dark_genes"] = dg

    out_path = os.path.join(output_dir, "report.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)
    logger.info("[sgnipt_report] report.json written to %s", out_path)
    return out_path


# --------------------------------------------------------------------------- #
# Public: generate_sgnipt_report_pdf
# --------------------------------------------------------------------------- #

def generate_sgnipt_report_pdf(
    report_json_path: str,
    output_dir: str,
    *,
    template_dir: Optional[str] = None,
    languages: Optional[List[str]] = None,
) -> List[str]:
    """
    Render sgnipt_<LANG>.html Jinja2 template → WeasyPrint PDF.
    Returns list of written PDF paths.
    """
    if languages is None:
        languages = ["EN"]

    try:
        with open(report_json_path, encoding="utf-8") as f:
            report_data = json.load(f)
    except Exception as e:
        logger.error("[sgnipt_report] Cannot read report.json %s: %s", report_json_path, e)
        return []

    # Resolve template directory
    candidates: List[str] = []
    if template_dir:
        candidates.append(template_dir)
    # Bundled templates: <repo_root>/data/report_templates
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    for _up in range(5):
        candidate = os.path.join(_self_dir, *[".."] * _up, "data", "report_templates")
        candidate = os.path.normpath(candidate)
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "sgnipt_EN.html")):
            candidates.append(candidate)
            break

    resolved_template_dir: Optional[str] = None
    for c in candidates:
        if os.path.isdir(c):
            resolved_template_dir = c
            break

    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        from weasyprint import CSS, HTML  # type: ignore
    except ImportError as e:
        logger.error("[sgnipt_report] Missing dependency (jinja2 / weasyprint): %s", e)
        return []

    os.makedirs(output_dir, exist_ok=True)
    pdf_paths: List[str] = []

    for lang in languages:
        lang_up = lang.upper()
        if lang_up not in SGNIPT_SUPPORTED_LANGUAGES:
            logger.warning("[sgnipt_report] Language %s not supported, falling back to EN", lang)
            lang_up = "EN"

        tpl_name = f"sgnipt_{lang_up}.html"
        tpl_html: Optional[str] = None

        if resolved_template_dir:
            tpl_path = os.path.join(resolved_template_dir, tpl_name)
            if os.path.isfile(tpl_path):
                try:
                    env = Environment(
                        loader=FileSystemLoader(resolved_template_dir),
                        autoescape=select_autoescape(["html"]),
                    )
                    env.filters["safe"] = lambda x: x  # allow |safe in template
                    tpl = env.get_template(tpl_name)
                    tpl_html = tpl.render(data=report_data)
                except Exception as e:
                    logger.error("[sgnipt_report] Jinja2 render failed for %s: %s", tpl_path, e)

        if tpl_html is None:
            logger.error("[sgnipt_report] Template %s not found in %s", tpl_name, resolved_template_dir)
            continue

        pdf_name = f"report_{lang_up}.pdf"
        pdf_path = os.path.join(output_dir, pdf_name)
        try:
            base_url = resolved_template_dir or output_dir
            html_obj = HTML(string=tpl_html, base_url=base_url)
            html_obj.write_pdf(pdf_path)
            pdf_paths.append(pdf_path)
            logger.info("[sgnipt_report] PDF written: %s", pdf_path)
        except Exception as e:
            logger.error("[sgnipt_report] WeasyPrint failed for %s: %s", pdf_path, e)

    return pdf_paths


# --------------------------------------------------------------------------- #
# Public: render_sgnipt_preview_html
# --------------------------------------------------------------------------- #

def render_sgnipt_preview_html(
    report_data: Dict[str, Any],
    *,
    template_dir: Optional[str] = None,
    language: str = "EN",
) -> str:
    """
    Render report_data dict → HTML string (no disk writes, for portal preview).
    """
    lang_up = language.upper()
    if lang_up not in SGNIPT_SUPPORTED_LANGUAGES:
        lang_up = "EN"

    tpl_name = f"sgnipt_{lang_up}.html"
    resolved_template_dir: Optional[str] = template_dir

    if not resolved_template_dir:
        _self_dir = os.path.dirname(os.path.abspath(__file__))
        for _up in range(5):
            candidate = os.path.normpath(os.path.join(_self_dir, *[".."] * _up, "data", "report_templates"))
            if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, tpl_name)):
                resolved_template_dir = candidate
                break

    if not resolved_template_dir or not os.path.isfile(os.path.join(resolved_template_dir, tpl_name)):
        raise FileNotFoundError(f"sgNIPT template not found: {tpl_name} (searched: {resolved_template_dir})")

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(resolved_template_dir),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template(tpl_name)
    return tpl.render(data=report_data)
