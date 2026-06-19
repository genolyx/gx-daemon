"""PGx PDF inclusion filtering (reviewer ✓ Include)."""

import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_pgx_report():
    path = os.path.join(_ROOT, "app", "services", "carrier_screening", "pgx_report.py")
    spec = importlib.util.spec_from_file_location("pgx_report_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_pgx = _load_pgx_report()
pgx_for_pdf = _pgx.pgx_for_pdf


def _pharmcat_row(gene: str, *, confirmed: bool = False) -> dict:
    return {
        "gene": gene,
        "diplotype": "*1/*1",
        "phenotype": "Normal",
        "category": "normal",
        "reviewer_confirmed": confirmed,
    }


def _custom_row(gene: str, rsid: str, *, confirmed: bool = False) -> dict:
    return {
        "gene": gene,
        "rsid": rsid,
        "genotype": "C/T",
        "reviewer_confirmed": confirmed,
    }


def test_pgx_for_pdf_apoe_only_after_save_omits_pharmcat():
    """Proactive: only APOE checked — PharmCAT table must not backfill all defaults."""
    pgx = {
        "status": "ok",
        "summary_text": "PharmCAT summary",
        "portal_review": {
            "include_apoe_proactive_pdf": True,
            "inclusions_saved": True,
        },
        "gene_results": [
            _pharmcat_row("CYP2C19"),
            _pharmcat_row("CYP2D6"),
            _pharmcat_row("SLCO1B1"),
        ],
        "custom_gene_results": [
            _custom_row("APOE", "rs429358", confirmed=True),
            _custom_row("APOE", "rs7412", confirmed=True),
            _custom_row("ABCB1", "rs1045642"),
        ],
        "drug_recommendations": [
            {"gene": "CYP2C19", "drug": "clopidogrel"},
            {"gene": "SLCO1B1", "drug": "simvastatin"},
        ],
    }
    out = pgx_for_pdf(pgx)
    assert out["gene_results"] == []
    assert len(out["custom_gene_results"]) == 2
    assert all(r["gene"] == "APOE" for r in out["custom_gene_results"])
    assert out["drug_recommendations"] == []


def test_pgx_for_pdf_unsaved_review_shows_all_when_none_checked():
    pgx = {
        "status": "ok",
        "summary_text": "PharmCAT summary",
        "gene_results": [
            _pharmcat_row("CYP2C19"),
            _pharmcat_row("CYP2D6"),
        ],
        "custom_gene_results": [
            _custom_row("ABCB1", "rs1045642"),
        ],
    }
    out = pgx_for_pdf(pgx)
    assert len(out["gene_results"]) == 2
    assert len(out["custom_gene_results"]) == 1


def test_pgx_for_pdf_proactive_default_apoe_shows_pharmcat_without_review():
    """Proactive default APOE must not hide PharmCAT rows before reviewer save."""
    pgx = {
        "status": "ok",
        "summary_text": "PharmCAT summary",
        "gene_results": [
            _pharmcat_row("CYP2C19"),
            _pharmcat_row("CYP2D6"),
        ],
        "custom_gene_results": [
            _custom_row("APOE", "rs429358"),
            _custom_row("APOE", "rs7412"),
        ],
    }
    out = pgx_for_pdf(pgx, default_include_apoe_proactive=True)
    assert len(out["gene_results"]) == 2
    assert (out.get("apoe_proactive_summary_html") or "").strip()


def test_pgx_for_pdf_saved_review_shows_only_confirmed_pharmcat():
    pgx = {
        "status": "ok",
        "summary_text": "PharmCAT summary",
        "portal_review": {"inclusions_saved": True, "include_apoe_proactive_pdf": False},
        "gene_results": [
            _pharmcat_row("CYP2C19", confirmed=True),
            _pharmcat_row("CYP2D6"),
        ],
        "custom_gene_results": [],
    }
    out = pgx_for_pdf(pgx)
    assert len(out["gene_results"]) == 1
    assert out["gene_results"][0]["gene"] == "CYP2C19"


def test_localize_pgx_does_not_rebuild_apoe_when_pdf_html_empty():
    """CN localization must not resurrect APOE when reviewer excluded it from the PDF."""
    localize_pgx_for_language = _pgx.localize_pgx_for_language
    data = {
        "pgx": {
            "apoe_diplotype_for_report": {"report_key": "e2_e2"},
            "apoe_proactive_summary_html": "",
            "gene_results": [],
        }
    }
    out = localize_pgx_for_language(data, "CN")
    assert not (out["pgx"].get("apoe_proactive_summary_html") or "").strip()
