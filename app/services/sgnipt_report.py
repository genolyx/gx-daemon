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

import base64
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
SGNIPT_LOGO_FILENAME = "genolyx_logo.png"


def _resolve_sgnipt_template_dir(template_dir: Optional[str] = None) -> Optional[str]:
    if template_dir and os.path.isdir(template_dir):
        return template_dir
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    for _up in range(5):
        candidate = os.path.normpath(os.path.join(_self_dir, *[".."] * _up, "data", "report_templates"))
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "sgnipt_EN.html")):
            return candidate
    return None


def _report_logo_src(template_dir: Optional[str] = None) -> str:
    """Logo for PDF + browser preview (data URI when file is available)."""
    candidates: List[str] = []
    try:
        from app.config import settings

        configured = (getattr(settings, "report_logo_path", None) or "").strip()
        if configured:
            candidates.append(configured)
        tpl_dir = (getattr(settings, "report_template_dir", None) or "").strip()
        if tpl_dir:
            candidates.append(os.path.join(tpl_dir, SGNIPT_LOGO_FILENAME))
    except Exception:
        pass
    resolved = _resolve_sgnipt_template_dir(template_dir)
    if resolved:
        candidates.append(os.path.join(resolved, SGNIPT_LOGO_FILENAME))
    candidates.extend([
        "/home/ken/gx-daemon/data/report_templates/genolyx_logo.png",
    ])
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    encoded = base64.standard_b64encode(f.read()).decode("ascii")
                return f"data:image/png;base64,{encoded}"
            except OSError as e:
                logger.warning("[sgnipt_report] Could not read logo %s: %s", path, e)
    if resolved and os.path.isfile(os.path.join(resolved, SGNIPT_LOGO_FILENAME)):
        return SGNIPT_LOGO_FILENAME
    return ""


def _inline_logo_in_html(html: str, template_dir: Optional[str] = None) -> str:
    """Replace relative logo path with embedded data URI (preview + PDF)."""
    logo_src = _report_logo_src(template_dir)
    if logo_src.startswith("data:"):
        return html.replace(f'src="{SGNIPT_LOGO_FILENAME}"', f'src="{logo_src}"')
    return html


def _attach_report_logo(report_data: Dict[str, Any], template_dir: Optional[str] = None) -> None:
    meta = report_data.setdefault("report_metadata", {})
    meta["logo_src"] = _report_logo_src(template_dir) or SGNIPT_LOGO_FILENAME

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
    clinvar_annotator: Optional[Any] = None,
    gnomad_annotator: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Merge gene knowledge + ClinVar annotation + ACMG rule-based classification
    into a confirmed variant dict. Falls back gracefully when resources absent.
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

    # ── ClinVar annotation ─────────────────────────────────────────────────
    chrom = str(v.get("chrom") or "")
    pos_val = v.get("pos")
    ref = str(v.get("ref") or "")
    alt = str(v.get("alt") or "")

    clinvar_result: Optional[Dict[str, Any]] = None
    if clinvar_annotator and chrom and pos_val is not None and ref and alt:
        try:
            clinvar_result = clinvar_annotator.lookup(chrom, int(pos_val), ref, alt)
        except Exception as e:
            logger.debug("[sgnipt_report] ClinVar lookup failed %s:%s: %s", chrom, pos_val, e)

    if clinvar_result:
        v["clinvar_sig"] = clinvar_result.get("clnsig", "")
        v["clinvar_sig_primary"] = clinvar_result.get("clnsig_primary", "")
        v["clinvar_stars"] = int(clinvar_result.get("stars") or 0)
        v["clinvar_dn"] = clinvar_result.get("clndn", "")
        v["clinvar_variation_id"] = clinvar_result.get("variation_id", "")
        v["clinvar_revstat"] = clinvar_result.get("revstat", "")
    else:
        v.setdefault("clinvar_sig", "")
        v.setdefault("clinvar_sig_primary", "")
        v.setdefault("clinvar_stars", 0)
        v.setdefault("clinvar_dn", "")

    # ── gnomAD annotation ──────────────────────────────────────────────────
    gnomad_af: Optional[float] = None
    if gnomad_annotator and chrom and pos_val is not None and ref and alt:
        try:
            gn = gnomad_annotator.lookup(chrom, int(pos_val), ref, alt)
            gnomad_af = gn.get("af")
        except Exception as e:
            logger.debug("[sgnipt_report] gnomAD lookup failed %s:%s: %s", chrom, pos_val, e)

    if gnomad_af is not None:
        v["gnomad_af"] = gnomad_af
    else:
        v.setdefault("gnomad_af", None)

    # ── ACMG rule-based classification ────────────────────────────────────
    if not v.get("acmg_classification"):
        try:
            from .carrier_screening.acmg import classify_acmg_lite
            acmg_input = {
                "chrom": chrom,
                "pos": pos_val,
                "ref": ref,
                "alt": alt,
                "gene": gene,
                "effect": v.get("effect") or "",
                "clinvar_sig_primary": v.get("clinvar_sig_primary") or "",
                "clinvar_stars": v.get("clinvar_stars") or 0,
                "gnomad_af": gnomad_af,
            }
            acmg_result = classify_acmg_lite(acmg_input)
            v["acmg_classification"] = acmg_result.get("classification", "VUS")
            v["acmg_criteria"] = acmg_result.get("criteria_met", [])
            v["acmg_reasoning"] = acmg_result.get("reasoning", "")
            v["acmg_confidence"] = acmg_result.get("confidence", "low")
        except Exception as e:
            logger.debug("[sgnipt_report] ACMG classification failed: %s", e)
            v.setdefault("acmg_classification", "")

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
    clinvar_vcf: Optional[str] = None,
    gnomad_dir: Optional[str] = None,
    gnomad_genomes_glob: str = "gnomad.genomes.v*.sites*.bgz",
    gnomad_exomes_glob: str = "gnomad.exomes.v*.sites*.bgz",
) -> str:
    """
    Build report.json from result.json + reviewer-confirmed variants.
    Annotates each confirmed variant with ClinVar + gnomAD + ACMG rule-based
    classification when annotation resources are configured.
    Returns the path to the written report.json.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        with open(result_json_path, encoding="utf-8") as f:
            result_data: Dict[str, Any] = json.load(f)
    except Exception as e:
        raise RuntimeError(f"[sgnipt_report] Cannot read result.json at {result_json_path}: {e}") from e

    # Build annotators (lazy — skip if paths not configured)
    clinvar_annotator: Optional[Any] = None
    gnomad_annotator: Optional[Any] = None
    try:
        from .carrier_screening.annotator import ClinVarAnnotator, GnomADAnnotator
        if clinvar_vcf and os.path.isfile(clinvar_vcf):
            clinvar_annotator = ClinVarAnnotator(clinvar_vcf)
            logger.info("[sgnipt_report] ClinVar annotator ready: %s", clinvar_vcf)
        else:
            if clinvar_vcf:
                logger.warning("[sgnipt_report] ClinVar VCF not found, skipping ClinVar: %s", clinvar_vcf)
            else:
                logger.info("[sgnipt_report] ClinVar VCF not configured, skipping ClinVar annotation")
        if gnomad_dir and os.path.isdir(gnomad_dir):
            gnomad_annotator = GnomADAnnotator(gnomad_dir, gnomad_genomes_glob, gnomad_exomes_glob)
            logger.info("[sgnipt_report] gnomAD annotator ready: %s", gnomad_dir)
        else:
            if gnomad_dir:
                logger.warning("[sgnipt_report] gnomAD dir not found, skipping gnomAD: %s", gnomad_dir)
    except Exception as e:
        logger.warning("[sgnipt_report] Failed to initialise annotators: %s", e)

    pi = patient_info or {}
    ri = reviewer_info or {}
    now_str = _fmt_date()

    # Enrich confirmed variants (gene knowledge + ClinVar + ACMG)
    enriched_variants = [
        _enrich_confirmed_variant(
            cv, result_data, gene_knowledge_db, gemini_api_key, gemini_model,
            clinvar_annotator=clinvar_annotator,
            gnomad_annotator=gnomad_annotator,
        )
        for cv in (confirmed_variants or [])
    ]

    overall_status = result_data.get("sgnipt_status") or "NO_CALL"
    # If any confirmed variant is classified Pathogenic/Likely Pathogenic → POSITIVE
    for cv in enriched_variants:
        acmg_cls = (cv.get("acmg_classification") or cv.get("classification") or "").lower()
        if "pathogenic" in acmg_cls:
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

    _attach_report_logo(report_data)

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
    resolved_template_dir = _resolve_sgnipt_template_dir(template_dir)
    if resolved_template_dir:
        candidates.append(resolved_template_dir)

    resolved_template_dir = None
    for c in candidates:
        if os.path.isdir(c):
            resolved_template_dir = c
            break

    _attach_report_logo(report_data, resolved_template_dir)

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
                    tpl_html = _inline_logo_in_html(tpl_html, resolved_template_dir)
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
        resolved_template_dir = _resolve_sgnipt_template_dir()

    if not resolved_template_dir or not os.path.isfile(os.path.join(resolved_template_dir, tpl_name)):
        raise FileNotFoundError(f"sgNIPT template not found: {tpl_name} (searched: {resolved_template_dir})")

    _attach_report_logo(report_data, resolved_template_dir)

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(resolved_template_dir),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template(tpl_name)
    html = tpl.render(data=report_data)
    return _inline_logo_in_html(html, resolved_template_dir)
