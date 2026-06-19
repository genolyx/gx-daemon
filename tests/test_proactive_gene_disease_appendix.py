"""Proactive appendix: baked-in template includes + mapping helpers."""

import importlib.util
import json
import os
import subprocess
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INCLUDES = os.path.join(_ROOT, "data", "report_templates", "includes")


def _load_report():
    path = os.path.join(_ROOT, "app", "services", "carrier_screening", "report.py")
    spec = importlib.util.spec_from_file_location("report_appendix_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_r = _load_report()


def test_baked_in_appendix_includes_exist():
    for lang in ("EN", "CN", "KO"):
        path = os.path.join(_INCLUDES, f"proactive_appendix_genes_{lang}.html")
        assert os.path.isfile(path), path
        text = open(path, encoding="utf-8").read()
        assert "BRCA1" in text
        assert "Cancer predisposition" in text or "肿瘤易感" in text or "암 발생 소인" in text
        assert "appendix-section-pgx" in text
        assert "Representative drug" in text or "代表性相关药物" in text or "대표 관련 약물" in text
        assert "APOE" in text


def test_proactive_report_metadata_include_pgx_despite_order_false(tmp_path):
    """Proactive PDF always includes PGx; legacy carrier.include_pgx=false must not hide results."""
    rj = _r.generate_report_json(
        order_id="proactive_pgx_meta_test",
        sample_name="sample",
        confirmed_variants=[],
        reviewer_info={},
        qc_summary={},
        output_dir=str(tmp_path),
        order_params={
            "carrier": {
                "package_code": "HealthScreening",
                "wes_panel_id": "proactive_health",
                "include_pgx": False,
            }
        },
        pdf_template_kind="proactive",
    )
    meta = json.load(open(rj, encoding="utf-8"))["report_metadata"]
    assert meta["pdf_template_kind"] == "proactive"
    assert meta["include_pgx"] is True


def test_build_interpretation_gene_disease_rows_from_mapping():
    mapping = {
        "diseases": [
            {
                "disease_name": "Cystic Fibrosis",
                "disease_name_cn": "囊性纤维化",
                "genes": ["CFTR"],
                "inheritance": "AR",
            },
            {
                "disease_name": "Spinal Muscular Atrophy",
                "genes": ["SMN1"],
            },
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(mapping, f)
        path = f.name
    try:
        rows = _r.build_interpretation_gene_disease_rows(
            ["SMN1", "CFTR"],
            disease_gene_json=path,
            lang="EN",
        )
        by_gene = {r["gene"]: r["disorder"] for r in rows}
        assert by_gene["CFTR"] == "Cystic Fibrosis"
        assert by_gene["SMN1"] == "Spinal Muscular Atrophy"
    finally:
        os.unlink(path)


def test_build_interpretation_gene_disease_groups_dual_column():
    proactive = {
        "categories": [
            {"id": "cancer", "label_en": "Cancer predisposition", "order": 1},
            {"id": "cardio", "label_en": "Cardiovascular", "order": 2},
        ],
        "diseases": [
            {"disease_name": "HBOC", "genes": ["BRCA1"], "category": "cancer"},
            {"disease_name": "HBOC", "genes": ["BRCA2"], "category": "cancer"},
            {"disease_name": "Marfan", "genes": ["FBN1"], "category": "cardio"},
            {"disease_name": "HCM", "genes": ["MYH7"], "category": "cardio"},
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(proactive, f)
        path = f.name
    try:
        groups = _r.build_interpretation_gene_disease_groups(
            ["BRCA1", "BRCA2", "FBN1", "MYH7"],
            proactive_disease_gene_json=path,
            lang="EN",
        )
        assert len(groups) == 2
        assert groups[0]["title"] == "Cancer predisposition"
        assert groups[0]["pairs"][0]["left"]["gene"] == "BRCA1"
        assert groups[0]["pairs"][0]["right"]["gene"] == "BRCA2"
    finally:
        os.unlink(path)


def test_generate_proactive_appendix_script():
    script = os.path.join(_ROOT, "scripts", "generate_proactive_appendix_includes.py")
    assert os.path.isfile(script)
    subprocess.run(["python3", script], check=True, cwd=_ROOT)
    assert "APC" in open(os.path.join(_INCLUDES, "proactive_appendix_genes_EN.html"), encoding="utf-8").read()
