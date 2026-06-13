"""
NIPT HTML Report Generator

Jinja2 HTML template → WeasyPrint PDF.
Alternative to the legacy PPTX→LibreOffice PDF pipeline.

Template lives in data/nipt_report_html/GX_Report_Template.html
"""

import base64
import itertools
import logging
import os
import re
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader

from .config import settings

logger = logging.getLogger(__name__)

# ── Static reference data (identical to original generate_report.py) ────────

COMMON_TRISOMIES = [
    {"condition": "Trisomy 21 (Down Syndrome)", "result": "Low Risk"},
    {"condition": "Trisomy 18 (Edwards Syndrome)", "result": "Low Risk"},
    {"condition": "Trisomy 13 (Patau Syndrome)", "result": "Low Risk"},
]

ADDITIONAL_TRISOMIES = [
    {"condition": "Trisomy 9", "result": "Low Risk"},
    {"condition": "Trisomy 16", "result": "Low Risk"},
    {"condition": "Trisomy 22", "result": "Low Risk"},
]

OTHER_AUTOSOMAL = [
    {"condition": "All Other Autosomal Trisomy", "result": "Low Risk"},
]

SCA_CONDITIONS = [
    {"condition": "XO (Turner Syndrome)", "result": "Low Risk"},
    {"condition": "XXX (Trisomy X)", "result": "Low Risk"},
    {"condition": "XXY (Klinefelter Syndrome)", "result": "Low Risk"},
    {"condition": "XYY (Jacob Syndrome)", "result": "Low Risk"},
]

COMMON_MICRODELETIONS = [
    {"condition": "1p36 deletion syndrome", "result": "Low Risk"},
    {"condition": "Wolf-Hirschhorn syndrome", "result": "Low Risk"},
    {"condition": "Williams-Beuren syndrome", "result": "Low Risk"},
    {"condition": "Prader-willi/Angelman syndrome", "result": "Low Risk"},
    {"condition": "Cri Du Chat syndrome", "result": "Low Risk"},
    {"condition": "Jacobsen syndrome", "result": "Low Risk"},
    {"condition": "DiGeorge syndrome (22q11.2)", "result": "Low Risk"},
    {"condition": "2q33.1 deletion syndrome", "result": "Low Risk"},
]

COMPREHENSIVE_MICRODELETIONS = [
    "1p36 terminal region (includes GABRD) gain", "Partial trisomy 5p", "Partial trisomy 12p", "17q11.2 recurrent region (includes NF1) gain",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) loss", "Partial monosomy 5q", "Partial trisomy 12q", "17q11.2 recurrent region (includes NF1) loss",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) gain", "Partial trisomy 5q", "Monosomy 13q14", "17q12 recurrent (RCAD syndrome) region (includes HNF1B) gain",
    "1q43-q44 terminal region (includes AKT3)", "6q24 region (includes PLAGL1)", "Monosomy 13q21-qter", "17q12 recurrent (RCAD syndrome) region (includes HNF1B) loss",
    "Partial monosomy 1p", "Partial trisomy 6p", "Trisomy 13cen-q14", "17q21.31 recurrent region (includes KANSL1)",
    "Monosomy 1q21-q32", "Partial monosomy 6q", "Trisomy 13q21-qter", "17q23.1-q23.2 recurrent region (includes TBX2, TBX4) gain",
    "Monosomy 1q42-qter", "Partial trisomy 6q", "14q11.2 region including CHD8 and SUPT16H", "17q23.1-q23.2 recurrent region (includes TBX2, TBX4) loss",
    "Trisomy 1q23-qter", "7p22.1 region (includes ACTB)", "DLK1-MEG3 Intergenic Region", "SOX9 upstream enhancer region",
    "2p15-p16.1 region (includes BCL11A)", "7q11.23 recurrent (Williams-Beuren syndrome) region (includes ELN) gain", "Partial trisomy 14", "Trisomy 17pter-q21",
    "2p24.3 MYCN-DDX1 duplication region", "7q11.23 recurrent distal region (includes HIP1, YWHAG)", "Trisomy 14q24-qter", "Partial monosomy 18p",
    "2q11.2 recurrent region (includes ARID5A, TMEM127)", "7q36.3 ZRS (SHH cis-regulatory) duplication region (within LMBR1 intron 5)", "15q11.2-q13 recurrent (PWS/AS) region (Class I, BP1-BP3 and Class II, BP2-BP3) gain", "Partial tetrasomy 18p",
    "2q13 recurrent region (distal) (includes BCL2L11) gain", "Partial monosomy 7p", "15q13.3 recurrent region (BP4-BP5) (includes CHRNA7)", "Trisomy 18pter-q12",
    "2q13 recurrent region (distal) (includes BCL2L11) loss", "Partial trisomy 7p", "15q13.3 recurrent region (D-CHRNA7 to BP5) (includes CHRNA7, OTUD7A)", "Monosomy 18q21-qter",
    "Partial monosomy 2p", "Monosomy 7q32-qter", "15q24 recurrent region (LCR A-LCR C)", "Trisomy 18q12-qter",
    "Partial trisomy 2p", "Trisomy 7q21-q31", "15q24 recurrent region (LCR A-LCR D) (includes SIN3A)", "Partial monosomy 20p",
    "Partial monosomy 2q", "Trisomy 7q32-qter", "15q24 recurrent region (LCR C-LCR D) (includes SIN3A)", "Trisomy 20p",
    "Partial trisomy 2q", "8p23.1 recurrent region (includes GATA4) gain", "15q25.2 recurrent region (proximal, LCR-A/B-C) (includes RPS17)", "Partial monosomy 21",
    "3q24 Region (includes ZIC1)", "8p23.1 recurrent region (includes GATA4) loss", "Supernumerary inv dup (15)", "22q11.2 recurrent (DGS) region (proximal, A-B, A-C, or A-D) (includes TBX1) gain",
    "3q29 recurrent region (includes DLG1)", "Partial trisomy 8q", "Trisomy 15q22-qter", "22q11.2 recurrent region (central, B-D or C-D) (includes CRKL)",
    "Monosomy 3p11-p21", "Partial monosomy 9p", "16p11.2 recurrent region (distal, BP2-BP3) (includes SH2B1)", "22q11.2 recurrent region (distal type I, D-E or D-F)",
    "Monosomy 3p25-pter", "Partial trisomy 9p", "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) gain", "22q11.2 recurrent region (distal type III, D-G, D-H) (includes SMARCB1)",
    "Partial trisomy 3p", "Partial monosomy 9q", "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) loss", "22q11.2 recurrent region (distal type III, E-H or F-H) (includes SMARCB1)",
    "Partial monosomy 3q", "Partial trisomy 9q", "16p12.2 recurrent region (proximal) (includes EEF2K, CDR2)", "22q11.2 recurrent region (distal type III, F-G) (includes SMARCB1)",
    "Partial trisomy 3q", "10q22.3-q23.2 recurrent region (includes BMPR1A)", "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) gain", "22q11.21 recurrent (CES) region (includes CECR2)",
    "Partial monosomy 3p", "Partial monosomy 10p", "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) loss", "Xp22.31 recurrent region (includes STS)",
    "4p16.3 terminal (Wolf-Hirschhorn syndrome) region gain", "Partial trisomy 10p", "16p13.3 region (includes CREBBP)", "Xp21.2 region (includes NROB1)",
    "Partial monosomy 4p", "Partial monosomy 10q", "Partial trisomy 16p", "Xp11.23 region (includes MAOA and MAOB)",
    "Partial trisomy 4p", "Partial trisomy 10q", "Partial monosomy 16q", "Xp11.22-p11.23 recurrent region (includes SHROOM4)",
    "Monosomy 4q21-q31", "11p11.2 (Potocki-Shaffer syndrome) region (includes ALX4, EXT2)", "Partial trisomy 16q", "Xp11.22 region (includes HUWE1)",
    "Monosomy 4q31-qter", "11p13 (WAGR syndrome) region", "17p11.2 recurrent (SMS/PLS) region (includes RAI1) gain", "Xq25 region (includes STAG2) gain",
    "Partial trisomy 4q", "11q13.2-q13.4 recurrent region (includes SHANK2, FGF3)", "17p11.2 recurrent (SMS/PLS) region (includes RAI1) loss", "Xq25 region (includes STAG2) loss",
    "Partial monosomy 4p", "Partial trisomy 11p", "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) gain", "Xq28 recurrent region (includes GDI1)",
    "5p15 terminal (Cri du chat syndrome) region gain", "Partial monosomy 11q", "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) loss", "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) gain",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) gain", "Partial trisomy 11q", "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) gain", "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) loss",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) loss", "Partial monosomy 12p", "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) loss", "Xq28 region (includes MECP2) gain",
    "", "", "", "Xq28 region (includes MECP2) loss",
]

DEFAULT_ORDER_OPTIONS = {
    "include_common_trisomies": True,
    "include_additional_trisomies": True,
    "include_other_autosomal": True,
    "include_sex_chromosomes": True,
    "include_common_microdeletions": True,
    "include_comprehensive_microdeletions": True,
}

# ── Trisomy / MD result mapping (from report_json → row data) ───────────────

_TRISOMY_DISPLAY = {
    "T21": "Trisomy 21 (Down Syndrome)",
    "T18": "Trisomy 18 (Edwards Syndrome)",
    "T13": "Trisomy 13 (Patau Syndrome)",
    "T9": "Trisomy 9",
    "T16": "Trisomy 16",
    "T22": "Trisomy 22",
    "XO": "XO (Turner Syndrome)",
    "XXX": "XXX (Trisomy X)",
    "XXY": "XXY (Klinefelter Syndrome)",
    "XYY": "XYY (Jacob Syndrome)",
}

_MD_DISPLAY = {
    "MD1": "1p36 deletion syndrome",
    "MD2": "2q33.1 deletion syndrome",
    "MD3": "Wolf-Hirschhorn syndrome",
    "MD4": "Cri Du Chat syndrome",
    "MD5": "Williams-Beuren syndrome",
    "MD6": "Jacobsen syndrome",
    "MD7": "Prader-willi/Angelman syndrome",
    "MD8": "DiGeorge syndrome (22q11.2)",
}


def _chunk(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _apply_results_from_report_json(
    base_list: List[Dict[str, str]],
    report_json: Dict[str, Any],
    mapping: Dict[str, str],
) -> List[Dict[str, str]]:
    """Override default 'Low Risk' with actual results from report_json."""
    trisomy_result = report_json.get("trisomy_result", [])
    md_result = report_json.get("md_result", [])

    high_risk_keys = set()
    for entry in trisomy_result:
        if isinstance(entry, dict):
            high_risk_keys.add(entry.get("item", ""))
        elif isinstance(entry, str):
            high_risk_keys.add(entry)
    for entry in md_result:
        if isinstance(entry, dict):
            high_risk_keys.add(entry.get("item", ""))
        elif isinstance(entry, str):
            high_risk_keys.add(entry)

    display_to_key = {v: k for k, v in mapping.items()}
    result = []
    for item in base_list:
        cond = item["condition"]
        key = display_to_key.get(cond, "")
        actual_result = "High Risk" if key in high_risk_keys else item["result"]
        result.append({"condition": cond, "result": actual_result})
    return result


def _map_report_json_to_template_data(report_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map make_report_json() output (space-separated keys) to the HTML template
    placeholder names (underscore-separated).
    """
    rj = report_json

    result_val = str(rj.get("Result") or "Low Risk").strip()
    mdr_val = str(rj.get("MDResult") or "Low Risk").strip()
    combined_result = "High Risk" if "High Risk" in (result_val, mdr_val) else "Low Risk"

    interp = str(rj.get("Interpretation") or "").strip()
    mdi = str(rj.get("MDI") or "").strip()
    if result_val == "High Risk" and mdr_val == "High Risk":
        combined_interp = f"{interp}\n{mdi}" if mdi else interp
    elif result_val == "High Risk":
        combined_interp = interp
    elif mdr_val == "High Risk":
        combined_interp = mdi
    else:
        combined_interp = interp

    gender = rj.get("Gender") or ""
    ff = rj.get("FF") or ""
    yff = rj.get("YFF") or ""
    preg = str(rj.get("Pregnancy Type") or "").strip().lower()
    if preg == "twins":
        ff_yff = ff
    else:
        ff_yff = yff if str(gender).lower() == "male" else ff

    return {
        "Patient_Name": rj.get("Patient Name") or "",
        "DOB": rj.get("DOB") or "",
        "W": str(rj.get("W") or ""),
        "D": str(rj.get("D") or ""),
        "Pregnancy_Type": str(rj.get("Pregnancy Type") or "").capitalize(),
        "MRN": rj.get("MRN") or rj.get("Sample Barcode") or "",
        "Indication": rj.get("Indication") or "",
        "Sample_Number": rj.get("Sample Number") or rj.get("Sample ID") or "",
        "Doctor": rj.get("Doctor") or "",
        "Hospital": rj.get("Hospital") or "",
        "Draw": rj.get("Collection Date") or "",
        "Report": rj.get("Report Date") or "",
        "Order_ID": rj.get("Order ID") or "",
        "Result_MDResult": combined_result,
        "Gender": str(gender).capitalize(),
        "FF_YFF": f"{ff_yff}%" if ff_yff and "%" not in str(ff_yff) else str(ff_yff),
        "Interpretation_MDI": combined_interp,
    }


def _build_layout(
    report_json: Dict[str, Any],
    options: Optional[Dict[str, bool]] = None,
):
    """Build aneuploidy / microdeletion row lists for the Jinja2 template."""
    opts = {**DEFAULT_ORDER_OPTIONS, **(options or {})}

    ct = _apply_results_from_report_json(COMMON_TRISOMIES, report_json, _TRISOMY_DISPLAY)
    at = _apply_results_from_report_json(ADDITIONAL_TRISOMIES, report_json, _TRISOMY_DISPLAY)
    oa = OTHER_AUTOSOMAL[:]
    sca = _apply_results_from_report_json(SCA_CONDITIONS, report_json, _TRISOMY_DISPLAY)
    cmd = _apply_results_from_report_json(COMMON_MICRODELETIONS, report_json, _MD_DISPLAY)

    left_stack: list = []
    if opts["include_common_trisomies"]:
        left_stack.extend(ct)
    if opts["include_other_autosomal"]:
        left_stack.extend(oa)

    right_stack: list = []
    if opts["include_additional_trisomies"]:
        right_stack.extend(at)

    sca_bottom: list = []
    if opts["include_sex_chromosomes"]:
        if not opts["include_additional_trisomies"]:
            right_stack.extend(sca)
        else:
            sca_bottom.extend(sca)

    aneuploidy_rows: list = []
    for left, right in itertools.zip_longest(left_stack, right_stack):
        row: list = []
        if left:
            row.append(left)
        elif right:
            row.append({"condition": "", "result": ""})
        if right:
            row.append(right)
        aneuploidy_rows.append(row)

    if sca_bottom:
        aneuploidy_rows.extend(list(_chunk(sca_bottom, 2)))

    md_page1: list = []
    if opts["include_common_microdeletions"]:
        md_page1.extend(cmd)

    md_page3: list = []
    show_page3 = False
    if opts["include_comprehensive_microdeletions"]:
        md_page3.extend(COMPREHENSIVE_MICRODELETIONS)
        md_page1.append({"condition": "Other microdeletions/duplications", "result": "Low Risk"})
        show_page3 = True

    md_page1_rows = list(_chunk(md_page1, 2))
    if show_page3:
        md_page3_rows = list(_chunk(md_page3, 4))
    else:
        md_page3_rows = []
    total_pages = 3 if show_page3 else 2

    return aneuploidy_rows, md_page1_rows, md_page3_rows, show_page3, total_pages


def generate_html_report(
    report_json: Dict[str, Any],
    output_dir: str,
    *,
    template_dir: Optional[str] = None,
    order_options: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """
    Generate a NIPT PDF report from the HTML template engine.

    Args:
        report_json: Output of make_report_json() (or equivalent dict).
        output_dir: Directory to write generated files.
        template_dir: Override for settings.nipt_report_html_template_dir.
        order_options: Override include_* flags (default: all True).

    Returns:
        {"merged": <pdf_path>, "individual": [<pdf_path>], "html": <html_path>}
    """
    tpl_dir = template_dir or settings.nipt_report_html_template_dir
    if not os.path.isdir(tpl_dir):
        raise FileNotFoundError(f"HTML template directory not found: {tpl_dir}")

    tpl_file = "GX_Report_Template.html"
    if not os.path.isfile(os.path.join(tpl_dir, tpl_file)):
        raise FileNotFoundError(
            f"Template not found: {os.path.join(tpl_dir, tpl_file)}"
        )

    env = Environment(loader=FileSystemLoader(tpl_dir))
    template = env.get_template(tpl_file)

    tpl_data = _map_report_json_to_template_data(report_json)
    aneuploidy_rows, md_p1_rows, md_p3_rows, show_page3, total_pages = _build_layout(
        report_json, order_options
    )

    html_output = template.render(
        **tpl_data,
        aneuploidy_rows=aneuploidy_rows,
        microdeletion_page1_rows=md_p1_rows,
        microdeletion_page3_rows=md_p3_rows,
        show_page3=show_page3,
        total_pages=total_pages,
    )

    os.makedirs(output_dir, exist_ok=True)

    # 상대 경로 이미지를 base64 data URI로 인라인 임베드 → HTML 단독 파일로 완결
    def _inline_images(html: str, base_dir: str) -> str:
        def replacer(m: re.Match) -> str:
            src = m.group(1)
            if src.startswith(("http://", "https://", "data:")):
                return m.group(0)
            img_path = os.path.join(base_dir, src)
            if not os.path.isfile(img_path):
                return m.group(0)
            ext = os.path.splitext(src)[1].lstrip(".").lower()
            mime = {"jpg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f'src="data:image/{mime};base64,{b64}"'
        return re.sub(r'src="([^"]+)"', replacer, html)

    html_output = _inline_images(html_output, tpl_dir)

    order_id = str(report_json.get("Order ID", "report")).strip().replace(" ", "_")
    sample_id = str(report_json.get("Sample ID") or report_json.get("Sample Number") or "").strip().replace(" ", "_")
    base_name = f"{order_id}_{sample_id}" if sample_id else order_id

    html_path = os.path.join(output_dir, f"{base_name}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_output)
    logger.info("HTML report written: %s", html_path)

    pdf_path = os.path.join(output_dir, f"{base_name}.pdf")
    try:
        from weasyprint import HTML as WeasyprintHTML

        WeasyprintHTML(string=html_output, base_url=tpl_dir).write_pdf(pdf_path)
        logger.info("PDF report written: %s", pdf_path)
    except ImportError:
        logger.error("WeasyPrint not installed — PDF generation skipped")
        pdf_path = None
    except Exception as e:
        logger.error("PDF generation failed: %s", e)
        pdf_path = None

    individual = [pdf_path] if pdf_path else []

    return {
        "merged": pdf_path,
        "individual": individual,
        "html": html_path,
    }
