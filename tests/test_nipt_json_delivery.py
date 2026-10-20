"""NIPT JSON report delivery: one bundle, cutoff fields removed."""

import pytest

from app.report_functions import assemble_nipt_delivery_json, normalize_report_output


def test_normalize_report_output_defaults_to_pdf():
    assert normalize_report_output(None) == "pdf"
    assert normalize_report_output("") == "pdf"
    assert normalize_report_output(" JSON ") == "json"
    with pytest.raises(ValueError):
        normalize_report_output("html")


def test_bundle_keeps_review_and_drops_cutoffs():
    analysis = {
        "OrderInfo": {},
        "NIPT": {
            "review": {
                "reviewer1": {"Trisomy_result": "", "saved": False},
                "reviewer2": {"Trisomy_result": "", "saved": False},
            },
            "final_results": {"fetal_fraction_seqff": 12.1},
            "trisomy_details": {
                "orig": {
                    "result_table": {
                        "Chromosome 1": {
                            "Z-score": 1.227,
                            "UAR(%)": 8.415,
                            "Z-score threshold": "L < 2.18~2.7 < H",
                            "UAR threshold": "L < 8.45~8.5 < H",
                        }
                    }
                }
            },
            "quality_control": {
                "sequencing_metrics": {
                    "mapping_rate": {"value": "90%", "threshold": ">85%"}
                }
            },
            "final_results_qc": {
                "zscore_threshold": "5.5",
                "wc_threshold_z": "5.12",
            },
        }
    }
    review_body = {
        "NIPT": {
            "review": {
                "reviewer2": {
                    "Trisomy_result": "High Risk",
                    "Trisomy_comment": "Reviewed by Rina",
                    "MD_result": "Low Risk",
                    "MD_comment": "low risk",
                }
            },
            "final_results": {"trisomy_result": ["XYY"], "md_result": []},
        }
    }
    bundle = assemble_nipt_delivery_json(
        "GNPR26100001",
        generated_at="2026-10-19T09:50:18+09:00",
        client={"id": "client-1", "name": "PT. Prodia Widyahusada Tbk"},
        order={"Order ID": "GNPR26100001", "Hospital": "RSCM", "Package": "All Chromosomes, SCA, 149MD"},
        analysis=analysis,
        review_body=review_body,
    )

    row = bundle["analysis"]["NIPT"]["trisomy_details"]["orig"]["result_table"]["Chromosome 1"]
    assert row["Z-score"] == 1.227
    assert row["UAR(%)"] == 8.415
    assert "Z-score threshold" not in row
    assert "UAR threshold" not in row
    assert "zscore_threshold" not in bundle["analysis"]["NIPT"]["final_results_qc"]
    assert "wc_threshold_z" not in bundle["analysis"]["NIPT"]["final_results_qc"]
    assert bundle["analysis"]["NIPT"]["quality_control"]["sequencing_metrics"]["mapping_rate"]["threshold"] == ">85%"

    assert "OrderInfo" not in bundle["analysis"]
    assert "review" not in bundle["analysis"]["NIPT"]
    assert bundle["client"]["name"] == "PT. Prodia Widyahusada Tbk"
    assert bundle["order"]["Hospital"] == "RSCM"
    assert bundle["review"]["reviewer2"]["Trisomy_result"] == "High Risk"
    assert bundle["review"]["trisomy_result"][0]["item"] == "XYY"

    # Source analysis on disk is not rewritten.
    assert analysis["OrderInfo"] == {}
    assert "review" in analysis["NIPT"]
    assert "Z-score threshold" in analysis["NIPT"]["trisomy_details"]["orig"]["result_table"]["Chromosome 1"]
