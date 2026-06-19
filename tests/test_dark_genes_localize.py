"""Dark Genes CN localization (import module directly)."""

import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dark_genes():
    path = os.path.join(_ROOT, "app", "services", "carrier_screening", "dark_genes.py")
    spec = importlib.util.spec_from_file_location("dark_genes_loc_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_dg = _load_dark_genes()


def test_dark_genes_display_title_cn():
    assert _dg.dark_genes_display_title("SMAca CHECK", "CN") == "脊髓性肌萎縮"
    assert _dg.dark_genes_display_title("HBA ANALYSIS (Alpha Thalassemia - Dosage)", "CN") == "α地中海貧血"


def test_detailed_sections_to_pdf_html_cn_kv_labels():
    sections = [
        {
            "title": "SMAca CHECK",
            "body": "SMN1_CN=2\nSMN2_CN=2\nSilentCarrier=No",
            "kind": "normal",
        }
    ]
    views = [{"approved": True, "risk": "low", "notes": "", "reviewer_set": True}]
    html = _dg.detailed_sections_to_pdf_html(
        sections, views, filter_by_approval=True, lang="CN"
    )
    assert "脊髓性肌萎縮" in html
    assert "SMN1 拷貝數" in html
    assert "靜在帶因者" in html


def test_localize_dark_genes_rebuilds_html_without_gemini():
    report = {
        "dark_genes": {
            "status": "ok",
            "detailed_sections": [
                {
                    "title": "SMAca CHECK",
                    "body": "SMN1_CN=2\nSMN2_CN=2",
                    "kind": "normal",
                }
            ],
            "section_reviews": [
                {"approved": True, "risk": "low", "notes": "", "reviewer_set": True}
            ],
            "report_detailed_html": "<div>Spinal Muscular Atrophy</div>",
        }
    }
    out = _dg.localize_dark_genes_for_language(report, "CN", db_path="", allow_gemini=False)
    html = out["dark_genes"]["report_detailed_html"]
    assert "脊髓性肌萎縮" in html
    assert "Spinal Muscular Atrophy" not in html
