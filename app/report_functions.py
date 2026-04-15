import os
import re
import logging
import json
import asyncio
import subprocess
import pandas as pd
from datetime import datetime, date
from typing import Dict, Any, List, Optional
from pypdf import PdfWriter
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Pt

from .config import settings
from .platform_client import get_order_detail, get_client_detail, get_client_info, extract_work_dir

logger = logging.getLogger(__name__)

# 외부 MD 목록 정의
OTHER_MD_LIST = [
    "1p32-p31 deletion syndrome", "1q41-q42 deletion syndrome", "1q43-q44 deletion syndrome",
    "2p12-p11.2 deletion syndrome", "2p15-p16.1 deletion syndrome", "2q13 deletion syndrome",
    "2q13 duplication syndrome", "2q31.1 microdeletion syndrome", "2q31.1 duplication syndrome",
    "2q35 duplication syndrome", "3p25.3 deletion syndrome", "3pter-p25 deletion syndrome",
    "3q13.31 deletion syndrome", "Dandy-Walker syndrome (DWS)", "3q26 microduplication syndrome",
    "3q29 deletion syndrome", "4q21 deletion syndrome", "Axenfeld-Rieger syndrome type 1 (RIEG1)",
    "5p13 duplication syndrome", "5q12 deletion syndrome", "5q14.3 deletion (proximal) syndrome",
    "Sotos syndrome", "6p22 microdeletion syndrome", "6q11-q14 deletion syndrome",
    "6q24-q25 deletion syndrome", "Coffin-Siris syndrome 1 (CSS1)", "Chordoma",
    "Greig cephalopolysyndactyly syndrome (GCPS)", "7p22.1 microduplication syndrome",
    "7q11.23 deletion (distal) syndrome", "Williams-Beuren syndrome (WBS, duplication)",
    "Currarino syndrome", "7q36.3 duplication syndrome", "8p11.2 deletion syndrome",
    "8p23.1 deletion syndrome", "8q12 microduplication syndrome", "Nablus mask-like facial syndrome (NMLFS)",
    "Trichorhinophalangeal syndrome type 2 (TRPS2)", "9p deletion syndrome", "9p13 microdeletion syndrome",
    "9p24.3 deletion syndrome", "9q33.3q34.11 microdeletion syndrome", "Early infantile epileptic encephalopathy 4 (EIEE4)",
    "Kleefstra syndrome 1 (KLEFS1)", "10p11.21-p12.31 microdeletion syndrome", "DiGeorge (DSG2)",
    "10q22.3-q23.2 deletion syndrome", "Split-hand/foot malformation 3 (SHFM3)", "10q26 deletion syndrome",
    "Potocki-Shaffer syndrome", "WAGR syndrome", "WAGRO syndrome", "11q13.2-q13.4 deletion syndrome",
    "11q22.2-q22.3 microdeletion syndrome", "11q23 deletion syndrome", "12p12.1 microdeletion syndrome",
    "12q14 microdeletion syndrome", "12q15q21.1 microdeletion syndrome", "13q14 deletion syndrome",
    "14q11-q22 deletion syndrome", "Frias syndrome", "14q24.1-q24.3 microdeletion syndrome",
    "15q13.3 deletion syndrome {BP4 to BP5} (loss)", "15q13.3 deletion syndrome {BP4 to BP5} (gain)",
    "15q14 microdeletion syndrome", "15q25.2 deletion (proximal) syndrome", "15q26-qter deletion syndrome",
    "16p11.2-p12.2 microduplication syndrome", "16p12.2 deletion (proximal) syndrome",
    "16p13.11 duplication syndrome", "16p13.11 deletion syndrome",
    "Polycystic kidney disease, infantile severe, with tuberous sclerosis (PKDTS)",
    "Rubinstein-Taybi syndrome", "Alpha-thalassemia/intellectual disability syndrome, chromosome 16-related (ATR-16) syndrome",
    "16q22 deletion syndrome", "Smith-Magenis syndrome", "Yuan-Harel-Lupski syndrome (YUHAL)",
    "17p12 deletion syndrome", "17p12 duplication syndrome", "17p13.1 deletion syndrome",
    "Miller-Dieker lissencephaly syndrome (MDLS) (loss)", "Miller-Dieker lissencephaly syndrome (MDLS) (gain)",
    "17p13.3 telomeric duplication syndrome", "17q12 deletion syndrome", "17q21.31 deletion syndrome",
    "17q23.1-q23.2 deletion syndrome", "Tetrasomy 18p syndrome", "18p deletion syndrome", "18q deletion syndrome",
    "19p13 duplication syndrome", "19q13.11 microdeletion syndrome", "20p13 microdeletion syndrome",
    "21q22.11-q22.12 microdeletion syndrome", "22q11.2 deletion syndrome (distal, D-E/F)",
    "22q11.2 deletion syndrome (LCR22 B/C-D)", "22q13 deletion syndrome", "22q13 duplication syndrome",
    "Xp11.22 duplication syndrome", "Xp11.22-p11.23 duplication syndrome", "Xp11.23 microdeletion syndrome",
    "Xp11.3 deletion syndrome", "Xp21 microdeletion syndrome", "Xp21.2 microduplication syndrome",
    "Xp22.31 microdeletion syndrome", "Xq21 microdeletion syndrome", "Xq22.3 telomeric deletion syndrome",
    "Xq27.3-q28 duplication syndrome", "Xq28 deletion syndrome"
]

OTHER_MD_141_LIST = [
    "Partial monosomy 1p",
    "1p36 terminal region (includes GABRD) gain",
    "Monosomy 1q21-q32",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) gain",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) loss",
    "Trisomy 1q23-qter",
    "Monosomy 1q42-qter",
    "1q43q44 terminal region (includes AKT3)",
    "Partial monosomy 2p",
    "Partial trisomy 2p",
    "2p24.3 MYCN-DDX1 duplication region",
    "2p15p16.1 region (includes BCL11A)",
    "2q11.2 recurrent region (includes ARID5A, TMEM127)",
    "Partial monosomy 2q",
    "Partial trisomy 2q",
    "2q13 recurrent region (distal) (includes BCL2L11) gain",
    "2q13 recurrent region (distal) (includes BCL2L11) loss",
    "Partial trisomy 3p",
    "Partial monosomy 3p",
    "Monosomy 3p25-pter",
    "Monosomy 3p11-p21",
    "Partial monosomy 3q",
    "Partial trisomy 3q",
    "3q24 Region (includes ZIC1)",
    "3q29 recurrent region (includes DLG1)",
    "Partial monosomy 4p",
    "Partial trisomy 4p",
    "Partial monosomy 4p",
    "4p16.3 terminal (Wolf-Hirschhorn syndrome) region gain",
    "Partial trisomy 4q",
    "Monosomy 4q21-q31",
    "Monosomy 4q31-qter",
    "Partial trisomy 5p",
    "5p15 terminal (Cri du chat syndrome) region gain",
    "Partial monosomy 5q",
    "Partial trisomy 5q",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) gain",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) loss",
    "Partial trisomy 6p",
    "Partial trisomy 6q",
    "Partial monosomy 6q",
    "6q24 region (includes PLAGL1)",
    "Partial monosomy 7p",
    "Partial trisomy 7p",
    "7p22.1 region (includes ACTB)",
    "7q11.23 recurrent (Williams-Beuren syndrome) region (includes ELN) gain",
    "7q11.23 recurrent distal region (includes HIP1, YWHAG)",
    "Trisomy 7q21-q31",
    "monosomy 7q32-qter",
    "Trisomy 7q32-qter",
    "7q36.3 ZRS (SHH cis-regulatory) duplication region (within LMBR1 intron 5)",
    "8p23.1 recurrent region (includes GATA4) gain",
    "8p23.1 recurrent region (includes GATA4) loss",
    "Partial trisomy 8q",
    "Partial monosomy 9p",
    "Partial trisomy 9p",
    "Partial monosomy 9q",
    "Partial trisomy 9q",
    "Partial monosomy 10p",
    "Partial trisomy 10p",
    "Partial monosomy 10q",
    "Partial trisomy 10q",
    "10q22.3q23.2 recurrent region (includes BMPR1A)",
    "Partial trisomy 11p",
    "11p13 (WAGR syndrome) region",
    "11p11.2 (Potocki-Shaffer syndrome) region (includes ALX4, EXT2)",
    "Partial monosomy 11q",
    "Partial trisomy 11q",
    "11q13.2q13.4 recurrent region (includes SHANK2, FGFs)",
    "Partial monosomy 12p",
    "Partial trisomy 12p",
    "Partial trisomy 12q",
    "Monosomy 13q14",
    "Trisomy 13cen-q14",
    "Monosomy 13q21-qter",
    "Trisomy 13q21-qter",
    "Partial trisomy 14",
    "14q11.2 region including CHD8 and SUPT16H",
    "Trisomy 14q24-qter",
    "DLK1-MEG3 Intergenic Region",
    "Supernumerary inv dup (15)",
    "15q11.2q13 recurrent (PWS/AS) region (Class I, BP1-BP3 and Class II, BP2-BP3) gain",
    "15q13.3 recurrent region (BP4-BP5) (includes CHRNA7)",
    "15q13.3 recurrent region (D-CHRNA7 to BP5) (includes CHRNA7, OTUD7A)",
    "Trisomy 15q22-qter",
    "15q24 recurrent region (LCR A-LCR C)",
    "15q24 recurrent region (LCR A-LCR D) (includes SIN3A)",
    "15q24 recurrent region (LCR C-LCR D) (includes SIN3A)",
    "15q25.2 recurrent region (proximal, LCR-A/B-C) (includes RPS17)",
    "Partial trisomy 16p",
    "16p13.3 region (includes CREBBP)",
    "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) gain",
    "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) loss",
    "16p12.2 recurrent region (proximal) (includes EEF2K, CDR2)",
    "16p11.2 recurrent region (distal, BP2-BP3) (includes SH2B1)",
    "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) gain",
    "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) loss",
    "Partial monosomy 16q",
    "Partial trisomy 16q",
    "Trisomy 17pter-q21",
    "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) gain",
    "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) loss",
    "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) gain",
    "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) loss",
    "17p11.2 recurrent (SMS/PLS) region (includes RAI1) gain",
    "17p11.2 recurrent (SMS/PLS) region (includes RAI1) loss",
    "17q11.2 recurrent region (includes NF1) gain",
    "17q11.2 recurrent region (includes NF1) loss",
    "17q12 recurrent (RCAD syndrome) region (includes HNF1B) gain",
    "17q12 recurrent (RCAD syndrome) region (includes HNF1B) loss",
    "17q21.31 recurrent (KdVS) region (includes KANSL1)",
    "17q23.1q23.2 recurrent region (includes TBX2, TBX4) gain",
    "17q23.1q23.2 recurrent region (includes TBX2, TBX4) loss",
    "SOX9 upstream enhancer region",
    "Partial monosomy 18p",
    "Partial tetrasomy 18p",
    "Trisomy 18pter-q12",
    "Trisomy 18q12-qter",
    "Monosomy 18q21-qter",
    "Partial monosomy 20p",
    "Trisomy 20p",
    "Partial monosomy 21",
    "22q11.21 recurrent (CES) region (includes CECR2)",
    "22q11.2 recurrent (DGS) region (proximal, A-B, A-C, or A-D) (includes TBX1) gain",
    "22q11.2 recurrent region (central, B-D or C-D) (includes CRKL)",
    "22q11.2 recurrent region (distal type I, D-E or D-F)",
    "22q11.2 recurrent region (distal type III, D-G, D-H) (includes SMARCB1)",
    "22q11.2 recurrent region (distal type III, E-H or F-H) (includes SMARCB1)",
    "22q11.2 recurrent region (distal type III, F-G) (includes SMARCB1)",
    "Xp22.31 recurrent region (includes STS)",
    "Xp21.2 region (includes NR0B1)",
    "Xp11.23 region (includes MAOA and MAOB)",
    "Xp11.22p11.23 recurrent region (includes SHROOM4)",
    "Xp11.22 region (includes HUWE1)",
    "Xq25 region (includes STAG2) gain",
    "Xq25 region (includes STAG2) loss",
    "Xq28 region (includes MECP2) gain",
    "Xq28 region (includes MECP2) loss",
    "Xq28 recurrent region (includes GDI1)",
    "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) gain",
    "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) loss"
]

def calculate_age_from_birthdate(birth_str: Optional[str]) -> Optional[int]:
    """생년월일에서 나이 계산"""
    if not birth_str:
        return None
    try:
        # 'Z' 제거하고 ISO 포맷으로 파싱
        birth_str = birth_str.replace("Z", "")
        birth_date = datetime.fromisoformat(birth_str).date()

        today = date.today()
        age = today.year - birth_date.year - (
            (today.month, today.day) < (birth_date.month, birth_date.day)
        )
        return age
    except Exception as e:
        logger.error(f"Failed to parse birth date: {birth_str} -> {e}")
        return None

def convert_trisomy_to_dict(trisomy_results):
    return {item.get("item"): item for item in trisomy_results}

def extract_trisomy_and_md_data(body):
    """
    새로운 구조로 Trisomy와 MD 데이터를 추출

    Args:
        body: API 요청 body

    Returns:
        dict: trisomy_result, trisomy_others, md_result, md_others를 포함하는 딕셔너리
    """
    nipt = body.get("NIPT", {})
    final_results = nipt.get("final_results", {})

    # final_results에서 기본 결과 가져오기
    original_trisomy_result = final_results.get("trisomy_result", [])
    original_md_result = final_results.get("md_result", [])

    # Trisomy 처리
    trisomy_result, trisomy_others = process_trisomy_data(original_trisomy_result, nipt)

    # MD 처리
    md_result, md_others = process_md_data(original_md_result, nipt)

    return {
        "trisomy_result": trisomy_result,
        "trisomy_others": trisomy_others,
        "md_result": md_result,
        "md_others": md_others
    }

def process_trisomy_data(original_trisomy_result, nipt):
    """
    Trisomy 데이터 처리

    Args:
        original_trisomy_result: final_results의 trisomy_result 배열
        nipt: NIPT 전체 데이터

    Returns:
        tuple: (trisomy_result, trisomy_others)
    """
    trisomy_result = []
    trisomy_others = []

    # Trisomy 이름 매핑
    trisomy_mapping = {
        "Trisomy13": "T13",
        "Trisomy18": "T18",
        "Trisomy21": "T21",
        "Trisomy9": "T9",
        "Trisomy16": "T16",
        "Trisomy22": "T22",
        "XO": "XO",
        "XXX": "XXX",
        "XXY": "XXY",
        "XYY": "XYY",
        "other": "Other"
    }

    has_other = False

    for item in original_trisomy_result:
        if item == "other":
            has_other = True
            trisomy_result.append({
                "item": "Other",
                "risk_after": "-"
            })
        else:
            # Let it have same value for now
            risk_after = "90/100"
            mapped_name = trisomy_mapping.get(item, item)
            trisomy_result.append({
                "item": mapped_name,
                "risk_after": risk_after
            })

    # "other"가 있는 경우 trisomy_details에서 High Risk 항목 찾기
    if has_other:
        trisomy_others = extract_trisomy_others(nipt)

    return trisomy_result, trisomy_others

def extract_trisomy_others(nipt):
    """
    trisomy_details에서 High Risk인 Chromosome 찾기
    """
    trisomy_others = []
    trisomy_details = nipt.get("trisomy_details", {})

    # 대상 염색체 목록
    target_chromosomes = [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 14, 15, 17, 20]

    # "orig"와 "fetus" 섹션 처리
    for section in ["orig", "fetus"]:
        if section not in trisomy_details:
            continue

        section_data = trisomy_details[section]
        result_table = section_data.get("result_table", {})

        for chromosome_num in target_chromosomes:
            chromosome_key = f"Chromosome {chromosome_num}"

            if chromosome_key in result_table:
                chromosome_data = result_table[chromosome_key]
                ezd_detection = chromosome_data.get("EZD Detection", "")

                if ezd_detection == "High Risk":
                    # 중복 체크 (이미 있는지 확인)
                    item_name = f"T{chromosome_num}"
                    if not any(item["item"] == item_name for item in trisomy_others):
                        trisomy_others.append({
                            "item": item_name,
                            "result": "High Risk",
                            "risk_after": "90/100"  # 기본값
                        })

    return trisomy_others

# MD 매핑 테이블 추가
MD8_DISEASE_MAPPING = {
    "1p36 deletion syndrome": "MD1",
    "2q33.1 deletion syndrome": "MD2",
    "Wolf-Hirschhorn syndrome": "MD3",
    "Cri Du Chat syndrome": "MD4",
    "Williams-Beuren syndrome": "MD5",
    "Jacobsen syndrome": "MD6",
    "Prader-willi/Angelman syndrome": "MD7",
    "DiGeorge syndrome": "MD8",
}

def process_md_data(original_md_result, nipt):
    """
    MD 데이터 처리 (매핑 테이블 적용)

    Args:
        original_md_result: final_results의 md_result 배열
        nipt: NIPT 전체 데이터

    Returns:
        tuple: (md_result, md_others)
    """
    md_result = []
    md_others = []

    has_other_md = False

    for item in original_md_result:
        if item.startswith("other_md"):
            has_other_md = True
            md_result.append("Other MD")
        else:
            # 매핑 테이블을 사용해서 변환
            mapped_name = MD8_DISEASE_MAPPING.get(item, item)
            md_result.append(mapped_name)

    # "other_md"가 있는 경우 상세 결과에서 찾기
    if has_other_md:
        md_details = nipt.get("md_details", {})
        md_others = extract_md_others(md_details)

    return md_result, md_others

def extract_md_others(md_details: dict):
    md_others = []
    found_diseases = set()

    if not isinstance(md_details, dict):
        return md_others  # 잘못된 입력 방어

    for key in md_details:
        if key.endswith("_results") and key.startswith("md"):
            md_results = md_details[key]

            for section in ["orig", "fetus"]:
                section_data = md_results.get(section)
                if not section_data:
                    continue

                for item_key, md_item in section_data.items():
                    if not item_key.startswith("md") or not item_key[2:].isdigit():
                        continue

                    if md_item.get("checked"):
                        disease_name = md_item.get("disease_name", "")
                        if disease_name and disease_name not in found_diseases:
                            found_diseases.add(disease_name)
                            md_others.append(disease_name)
                            #md_others.append({
                            #    "item": disease_name,
                            #    "result": "High Risk"
                            #})
    return md_others

def format_indication(indication_list: Optional[List[str]], specify: Optional[str] = None) -> Optional[str]:
    """
    Indication ENUM을 사람이 읽을 수 있는 문장으로 변환

    Args:
        indication_list: indicationForTesting 리스트
        specify: indicationForTestingSpecify 값 (ETC일 때 사용)

    Returns:
        변환된 문자열 또는 None
    """
    if not indication_list:
        return None

    # ENUM to readable text 매핑
    indication_mapping = {
        "ELDERLY_PREGNANT_WOMEN": "Elderly Pregnant Women",
        "HIGH_RISK_SERUM_SCREENING": "High Risk Serum Screening",
        "ULTRASOUND_ABNORMALITIES": "Ultrasound Abnormalities",
        "FAMILY_HISTORY_CHROMOSOMAL_ABNORMALITY": "Family or History of Chromosomal Abnormality",
        "ETC": None  # ETC는 specify 값으로 대체
    }

    formatted_items = []
    for item in indication_list:
        if item == "ETC":
            # ETC인 경우 specify 값 사용
            if specify:
                formatted_items.append(specify)
        else:
            # 매핑된 값이 있으면 사용, 없으면 원본 사용
            mapped_value = indication_mapping.get(item, item)
            if mapped_value:
                formatted_items.append(mapped_value)

    if not formatted_items:
        return None

    return ", ".join(formatted_items)

def extract_report_data(body, show_gender="Yes"):
    nipt = body.get("NIPT", {})
    reviewer2 = nipt.get("review", {}).get("reviewer2", {})
    final_results = nipt.get("final_results", {})
    trisomy_results = nipt.get("trisomy_results", {})

    # 기존 trisomy_results를 딕셔너리로 변환 (호환성 유지)
    trisomy_dict = {item.get("item"): item for item in trisomy_results}

    # 새로운 Trisomy와 MD 데이터 추출
    trisomy_md_data = extract_trisomy_and_md_data(body)

    t21_ST = trisomy_dict.get("T21", {}).get("risk_before_single")
    t21_TT = trisomy_dict.get("T21", {}).get("risk_before_twin")
    t18_ST = trisomy_dict.get("T18", {}).get("risk_before_single")
    t18_TT = trisomy_dict.get("T18", {}).get("risk_before_twin")
    t13_ST = trisomy_dict.get("T13", {}).get("risk_before_single")
    t13_TT = trisomy_dict.get("T13", {}).get("risk_before_twin")

    if show_gender == "Yes":
        gender = final_results.get("fetal_gender")
    else:
        gender = "N/A"

    return {
        "FF": final_results.get("fetal_fraction_seqff"),
        "YFF": final_results.get("fetal_fraction_yff"),
        "Gender": final_results.get("fetal_gender"),
        "Result": reviewer2.get("Trisomy_result"),
        "MDResult": reviewer2.get("MD_result"),
        "ST21": t21_ST,
        "ST18": t18_ST,
        "ST13": t13_ST,
        "TT21": t21_TT,
        "TT18": t18_TT,
        "TT13": t13_TT,
        "Risk After": "<2/10,000",
        "Interpretation": reviewer2.get("Trisomy_comment"),
        "MDI": reviewer2.get("MD_comment"),
        "trisomy_result": trisomy_md_data["trisomy_result"],
        "trisomy_others": trisomy_md_data["trisomy_others"],
        "md_result": trisomy_md_data["md_result"],
        "md_others": trisomy_md_data["md_others"]
    }



async def make_report_json(order_id: str, review_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    주문 정보와 리뷰 데이터를 결합하여 리포트용 JSON 생성
    """
    logger.info(f"Creating report JSON for order: {order_id}")

    try:
        # 1. 주문 상세 정보 조회
        from .aws_client import get_order_detail
        order = await get_order_detail(order_id)
        logger.debug(f"Retrieved order details for {order_id}")
        logger.info(order)

        # Indication 필드 디버깅 로그
        logger.info(f"[make_report_json] Order {order_id} - order.indication value: {order.indication}")
        logger.info(f"[make_report_json] Order {order_id} - order.indication type: {type(order.indication)}")

        # 2. 클라이언트 정보 조회 (우선순위: clientId > partnerId > package.clientId)
        client = await get_client_info(order)

        if client:
            logger.debug(f"Retrieved client details: {client.name}")
        else:
            logger.warning("No client information found")


        # 3. 주문 정보 기반 필드 매핑
        report = {
            "Order ID": order.id,
            "Patient Name": order.patientName,
            "kg": order.weight,
            "cm": order.height,
            "Indication": format_indication(order.indication, order.indicationForTestingSpecify),
            "W": order.gestationalAgeWeeks,
            "D": order.gestationalAgeDays,
            "Pregnancy Type": str(order.pregnancyType).capitalize(),
            "Doctor": order.doctor if order.doctor else None,
            "Hospital": order.hospital.name if order.hospital else None,
            "Sample Type": str(order.sampleSpecimenType).capitalize(),
            "Sample Number": order.sampleId,
            "Sample ID": order.sampleId,
            "Sample Barcode": order.sampleBarcode, # For Medfield
            "MRN": order.medicalRecordId, # for Medfield
            "Kit ID": order.sequencingBatchId, # for PH
            "Package": order.package.code,
            "Resample": str(order.resample).capitalize(), # for PH
            "Test Requested": order.package.code,
            "Report Language": order.reportLanguage,
            "Client Name": client.name if client else None,
        }

        # 3. 계산된 필드 추가
        report["DOB"] = (
            datetime.fromisoformat(order.patientBirth.replace("Z", "+00:00")).strftime("%d-%b-%Y")
            if order.patientBirth else "-"
        )

        report["Collection Date"] = (
            datetime.fromisoformat(order.sampleCollectedAt.replace("Z", "+00:00")).strftime("%d-%b-%Y")
            if order.sampleCollectedAt else "-"
        )

        report["Receipt Date"] = (
            datetime.fromisoformat(order.receiptDate.replace("Z", "+00:00")).strftime("%d-%b-%Y")
            if order.receiptDate else "-"
        )

        report["A"] = calculate_age_from_birthdate(order.patientBirth)
        #report["Report Date"] = date.today().strftime("%Y-%m-%d")
        report["Report Date"] = date.today().strftime("%d-%b-%Y")
        report["TemplateKey"] = order.package.code
        logger.info(f"TemplateKey : {order.package.code}")

        extracted_data = extract_report_data(review_json, order.showFetalGender)
        report.update(extracted_data)

        logger.info(f"Report JSON created successfully for {order_id}")
        logger.info(f"[make_report_json] Order {order_id} - Final report['Indication']: {report.get('Indication')}")
        return report

    except Exception as e:
        logger.error(f"Error creating report JSON for {order_id}: {e}")
        raise

def assign_md_results(row: Dict[str, Any], package_code: Optional[str] = None) -> None:
    """MD 결과를 행 데이터에 할당 (md_result / md_others 전용)"""
    md_items = [f"MD{i}" for i in range(1, 9)]

    md_result_list = row.get("md_result", [])
    md_others_list = row.get("md_others", [])

    # PackageCode에 따라 사용할 MD 리스트 결정
    if package_code and package_code.strip().upper() in ["MD141", "GENOLYX"]:
        md_list = OTHER_MD_141_LIST
        logger.info(f"Using OTHER_MD_141_LIST for package: {package_code}")
    else:
        md_list = OTHER_MD_LIST
        logger.debug(f"Using OTHER_MD_LIST for package: {package_code}")

    high_risk_items = set()

    # md_result 처리
    for item in md_result_list:
        if isinstance(item, str):
            if item in md_items:
                high_risk_items.add(item)
            elif item == "Other MD":
                high_risk_items.add("Other MD")
        elif isinstance(item, dict) and item.get("result", "").lower() == "high risk":
            name = item.get("item", "")
            if name in md_items or name == "Other MD":
                high_risk_items.add(name)

    # md_others 처리 (새 구조: string 리스트)
    for item in md_others_list:
        if isinstance(item, str):
            if item in md_list:  # 선택된 리스트 사용
                high_risk_items.add(item)

    # 250816 : md87 reporting error
    other_md_from_result = ("Other MD" in high_risk_items)

    # 기본 초기화
    for item in md_items:
        row[item] = "High Risk" if item in high_risk_items else "Low Risk"

    for name in md_list:  # 선택된 리스트 사용
        row[name] = "High Risk" if name in high_risk_items else "Low Risk"
        if "Alpha-thalassemia" in name:
            row["Alpha-thalassemia"] = row[name]

    # Other MD = (md_result에 Other MD가 있었거나) OR (md_list 중 하나라도 High Risk)
    row["Other MD"] = "High Risk" if (
        other_md_from_result or any(row.get(name, "").lower() == "high risk" for name in md_list)
    ) else "Low Risk"

    # Twins 성별 처리
    gender = str(row.get("Gender", "")).strip().lower()
    pregnancy_type = str(row.get("Pregnancy Type", "")).strip().lower()
    if pregnancy_type == "twins":
        if gender == "male":
            row["Gender"] = {"text": "Y Chromosome Detected", "font_size": 10}
        elif gender == "female":
            row["Gender"] = {"text": "Y Chromosome Not Detected", "font_size": 10}

def assign_md_results_old(row: Dict[str, Any]) -> None:
    """MD 결과를 행 데이터에 할당 (새 구조 및 유연한 구조 지원)"""
    md_result = str(row.get("MDResult", "Low Risk")).strip().lower()
    md_items = ["MD1", "MD2", "MD3", "MD4", "MD5", "MD6", "MD7", "MD8"]

    # 입력 구조 유연성 고려
    md_result_list = row.get("md_result", [])
    md_others_list = row.get("md_others", [])

    high_risk_items = set()

    # md_result 항목 처리
    for item in md_result_list:
        if isinstance(item, str):
            if item in md_items:
                high_risk_items.add(item)
        elif isinstance(item, dict):
            if item.get("result", "").strip().lower() == "high risk":
                name = item.get("item", "")
                if name in md_items:
                    high_risk_items.add(name)

    # md_others 항목 처리
    for item in md_others_list:
        if isinstance(item, str):
            # 해당 질환이 OTHER_MD_LIST 안에 있는 경우만 처리
            if item in OTHER_MD_LIST:
                high_risk_items.add(item)
        elif isinstance(item, dict):
            if item.get("result", "").strip().lower() == "high risk":
                name = item.get("item", "")
                if name in OTHER_MD_LIST:
                    high_risk_items.add(name)

    # 모든 기본 MD 필드 초기화
    for item in md_items:
        row[item] = "High Risk" if item in high_risk_items else "Low Risk"
    for name in OTHER_MD_LIST:
        if "Alpha-thalassemia" in name:
            row["Alpha-thalassemia"] if name in high_risk_items else "Low Risk"
        else:
            row[name] = "High Risk" if name in high_risk_items else "Low Risk"

    # Other MD는 OTHER_MD_LIST 중 하나라도 High Risk면 High Risk
    row["Other MD"] = "High Risk" if any(
        row.get(name, "").strip().lower() == "high risk" for name in OTHER_MD_LIST
    ) else "Low Risk"

    # 성별 필드 포맷 처리 (쌍둥이 케이스 포함)
    gender = str(row.get("Gender", "")).strip().lower()
    pregnancy_type = str(row.get("Pregnancy Type", "")).strip().lower()

    if pregnancy_type == "twins":
        if gender == "male":
            row["Gender"] = {"text": "Y Chromosome Detected", "font_size": 10}
        elif gender == "female":
            row["Gender"] = {"text": "Y Chromosome Not Detected", "font_size": 10}


def translate_risk(value: str, lang: str) -> str:
    """리스크 값을 언어별로 번역"""
    translations = {
        "ID": {"low risk": "Risiko Rendah", "high risk": "Risiko Tinggi"},
        "CN": {"low risk": "陰性: 未檢測異常", "high risk": "陽性: 檢測異常"}
    }
    return translations.get(lang, {}).get(value.lower(), value)

def replace_placeholder_in_paragraph(paragraph, data_row, report_lang):
    """
    Paragraph 전체에서 placeholder를 찾아 교체
    (PowerPoint의 run 분리 문제 해결)
    """
    # paragraph의 전체 텍스트 가져오기
    full_text = paragraph.text

    # 모든 placeholder 패턴 찾기 및 교체
    modified_text = full_text
    replacements_made = []
    font_size_to_apply = None  # 단일 placeholder인 경우에만 사용

    for key, value in data_row.items():
        # 공백 변형도 처리 (PPTX에서 공백이 추가될 수 있음)
        placeholder = f"<<{key}>>"
        placeholder_with_space = f"<<{key} >>"  # 끝에 공백 있는 버전

        if placeholder in modified_text or placeholder_with_space in modified_text:
            # 값 포맷팅
            if pd.isna(value) or str(value).strip() == "":
                replacement = ""
            elif isinstance(value, dict) and "text" in value:
                replacement = value["text"]
                # font_size 저장 (단일 placeholder 확인 후 적용)
                if "font_size" in value:
                    font_size_to_apply = value["font_size"]
            else:
                val_str = str(value).strip()
                val_lower = val_str.lower()

                if report_lang == "ID":
                    replacement = {
                        "low risk": "Risiko Rendah",
                        "high risk": "Risiko Tinggi",
                        "male": "Pria",
                        "female": "Perempuan"
                    }.get(val_lower, val_str)
                elif report_lang == "CN":
                    replacement = {"low risk": "陰性:未檢測異常", "high risk": "陽性:檢測異常"}.get(val_lower, val_str)
                else:
                    replacement = val_str

            # 두 가지 버전 모두 교체 시도
            if placeholder in modified_text:
                modified_text = modified_text.replace(placeholder, replacement)
                replacements_made.append((placeholder, replacement, key))
            if placeholder_with_space in modified_text:
                modified_text = modified_text.replace(placeholder_with_space, replacement)
                replacements_made.append((placeholder_with_space, replacement, key))

    # 변경사항이 있으면 paragraph 업데이트
    if modified_text != full_text:
        # 모든 run을 제거하고 새로운 텍스트로 교체
        for run in paragraph.runs[:]:
            run.text = ""

        if paragraph.runs:
            paragraph.runs[0].text = modified_text

            # 정확히 하나의 placeholder만 교체된 경우에만 font_size 적용
            # (여러 placeholder가 섞여있으면 전체에 영향을 주므로 적용 안함)
            unique_keys = set(r[2] for r in replacements_made)
            if len(unique_keys) == 1 and font_size_to_apply:
                paragraph.runs[0].font.size = Pt(font_size_to_apply)
        else:
            new_run = paragraph.add_run(modified_text)

            # 정확히 하나의 placeholder만 교체된 경우에만 font_size 적용
            unique_keys = set(r[2] for r in replacements_made)
            if len(unique_keys) == 1 and font_size_to_apply:
                new_run.font.size = Pt(font_size_to_apply)

def replace_placeholder_with_style(run, paragraph, placeholder, replacement, font_color=None, bold=None, font_size=None):
    """
    placeholder를 교체하고 해당 부분만 font style 적용
    (앞뒤 텍스트는 원래 스타일 유지)
    
    Returns:
        bool: 성공 여부
    """
    if placeholder not in run.text:
        return False
    
    # 중요: placeholder만 있는 경우는 기존 방식 사용 (run을 쪼개지 않음)
    # 이렇게 해야 기존 로직이 그대로 유지됨
    if run.text.strip() == placeholder.strip():
        run.text = replacement
        if font_color:
            run.font.color.rgb = font_color
        if bold is not None:
            run.font.bold = bold
        if font_size:
            run.font.size = font_size
        return True
    
    # placeholder가 다른 텍스트와 섞인 경우만 split 방식 사용
    parts = run.text.split(placeholder)
    if len(parts) != 2:
        # placeholder가 여러 개 있는 경우 - 기존 방식 사용
        run.text = run.text.replace(placeholder, replacement)
        if font_color:
            run.font.color.rgb = font_color
        if bold is not None:
            run.font.bold = bold
        if font_size:
            run.font.size = font_size
        return True
    
    before_text, after_text = parts
    
    # 앞뒤에 텍스트가 없으면 (공백만 있는 경우) 기존 방식 사용
    if not before_text.strip() and not after_text.strip():
        run.text = replacement
        if font_color:
            run.font.color.rgb = font_color
        if bold is not None:
            run.font.bold = bold
        if font_size:
            run.font.size = font_size
        return True
    
    # 실제로 앞뒤에 의미있는 텍스트가 있는 경우만 split
    # 원래 run의 font size 저장 (새 run에서 상속하기 위해)
    original_font_size = run.font.size
    
    # 기존 run은 앞 텍스트만 유지 (원래 스타일 유지)
    run.text = before_text
    
    # placeholder 값을 새 run으로 추가 (font style 적용)
    new_run = paragraph.add_run()
    new_run.text = replacement
    if font_color:
        new_run.font.color.rgb = font_color
    if bold is not None:
        new_run.font.bold = bold
    if font_size:
        new_run.font.size = font_size
    elif original_font_size:
        # font_size가 명시되지 않았으면 원래 run의 font size 유지
        new_run.font.size = original_font_size
    
    # 뒤 텍스트도 새 run으로 추가 (원래 스타일 유지)
    if after_text:
        after_run = paragraph.add_run()
        after_run.text = after_text
        if original_font_size:
            after_run.font.size = original_font_size
    
    return True

def replace_placeholder_in_run(run, paragraph, data_row, report_lang):
    mdr_val = (data_row.get("MDResult") or "low risk").strip().lower()
    result_val = (data_row.get("Result") or "").strip().lower()
    interp_val = str(data_row.get("Interpretation", "")).strip()
    mdi_val = str(data_row.get("MDI", "")).strip()

    client_name = str(data_row.get("Client Name", "")).strip().lower()
    is_cordlife = client_name.startswith("cordlife")
    is_testclient = client_name.startswith("testclient")

    if "<<Result_MDResult>>" in run.text:
        combined = "High Risk" if "high risk" in (result_val, mdr_val) else "Low Risk"
        translated = translate_risk(combined, report_lang)
        
        val_lower = combined.lower()
        if is_cordlife or is_testclient:
            if val_lower == "high risk":
                replace_placeholder_with_style(run, paragraph, "<<Result_MDResult>>", translated, 
                                              RGBColor(255, 0, 0), True)
            elif val_lower == "low risk":
                replace_placeholder_with_style(run, paragraph, "<<Result_MDResult>>", translated, 
                                              RGBColor(0, 128, 0), True)
        else:
            if val_lower == "high risk":
                replace_placeholder_with_style(run, paragraph, "<<Result_MDResult>>", translated, 
                                              RGBColor(255, 0, 0), False)
            elif val_lower == "low risk":
                replace_placeholder_with_style(run, paragraph, "<<Result_MDResult>>", translated, 
                                              RGBColor(0, 0, 0), False)
        return

    if "<<Interpretation_MDI>>" in run.text:
        both_high = result_val == "high risk" and mdr_val == "high risk"
        both_low = result_val == "low risk" and mdr_val == "low risk"

        if both_high:
            combined = f"{interp_val}\n{mdi_val}"
        elif result_val == "high risk":
            combined = interp_val
        elif mdr_val == "high risk":
            combined = mdi_val
        elif both_low:
            combined = interp_val
        else:
            combined = ""

        if is_cordlife or is_testclient:
            if "high risk" in combined.lower():
                replace_placeholder_with_style(run, paragraph, "<<Interpretation_MDI>>", combined, 
                                              RGBColor(255, 0, 0), True)
            else:
                replace_placeholder_with_style(run, paragraph, "<<Interpretation_MDI>>", combined, 
                                              RGBColor(0, 128, 0), True)
        else:
            replace_placeholder_with_style(run, paragraph, "<<Interpretation_MDI>>", combined, 
                                          RGBColor(0, 0, 0), False)
        return

    if "<<FF_YFF>>" in run.text:
        gender = str(data_row.get("Gender", "")).strip().lower()
        ff_val = str(data_row.get("FF", "")).strip()
        yff_val = str(data_row.get("YFF", "")).strip()
        combined_val = yff_val if gender == "male" else ff_val
        run.text = run.text.replace("<<FF_YFF>>", combined_val)

    preg_type = str(data_row.get("Pregnancy Type", "")).strip().lower()

    # 20250801
    if "<<FF_YFF>>" in run.text:
        ff_val = str(data_row.get("FF", "")).strip()
        yff_val = str(data_row.get("YFF", "")).strip()

        if preg_type == "twins":
            combined_val = ff_val
        else:
            gender = str(data_row.get("Gender", "")).strip().lower()
            combined_val = yff_val if gender == "male" else ff_val

        run.text = run.text.replace("<<FF_YFF>>", combined_val)


    for marker in ["21", "18", "13"]:
        placeholder = f"<<ST{marker}_TT{marker}>>"
        if placeholder in run.text:
            value = data_row.get(f"ST{marker}") if preg_type == "single" else data_row.get(f"TT{marker}")
            run.text = run.text.replace(placeholder, str(value) if value else "")

    for key, value in data_row.items():
        placeholder = f"<<{key}>>"
        if placeholder in run.text:
            if key == "MDI" and not (is_cordlife or is_testclient) and mdr_val != "high risk":
                run.text = run.text.replace(placeholder, "")
                continue
            if pd.isna(value) or str(value).strip() == "":
                run.text = run.text.replace(placeholder, "")
                continue
            if isinstance(value, dict) and "text" in value:
                # dict value (예: Gender with font_size)
                replace_placeholder_with_style(run, paragraph, placeholder, value["text"], 
                                              font_size=Pt(value.get("font_size", 18)))
                continue

            val_str = str(value).strip()
            val_lower = val_str.lower()

            if report_lang == "ID":
                formatted_value = {
                    "low risk": "Risiko Rendah",
                    "high risk": "Risiko Tinggi",
                    "male": "Pria",
                    "female": "Perempuan"
                }.get(val_lower, val_str)
            elif report_lang == "CN":
                formatted_value = {"low risk": "陰性:未檢測異常", "high risk": "陽性:檢測異常"}.get(val_lower, val_str)
            else:
                formatted_value = val_str

            # font style 결정
            font_color = None
            bold = None
            
            result_val_lower = result_val.lower()
            mdr_val_lower = mdr_val.lower()
            
            if key == "Interpretation":
                if is_cordlife or is_testclient:
                    if result_val_lower == "high risk":
                        font_color = RGBColor(255, 0, 0)
                        bold = True
                    elif result_val_lower == "low risk":
                        font_color = RGBColor(0, 128, 0)
                        bold = True
                else:
                    font_color = RGBColor(0, 0, 0)
                    bold = False
            elif key == "MDI":
                if is_cordlife or is_testclient:
                    if mdr_val_lower == "high risk":
                        font_color = RGBColor(255, 0, 0)
                        bold = True
                    elif mdr_val_lower == "low risk":
                        font_color = RGBColor(0, 128, 0)
                        bold = True
                else:
                    font_color = RGBColor(0, 0, 0)
                    bold = False
            elif val_lower == "high risk":
                font_color = RGBColor(255, 0, 0)
                bold = False
            elif val_lower == "low risk":
                font_color = RGBColor(0, 0, 0)
                bold = False
            
            # placeholder 교체 및 font style 적용
            if font_color or bold is not None:
                replace_placeholder_with_style(run, paragraph, placeholder, formatted_value, 
                                              font_color, bold)
            else:
                run.text = run.text.replace(placeholder, formatted_value)

def process_trisomy_md_placeholders(data_row: Dict[str, Any], package_code: Optional[str] = None) -> None:
    """
    새 구조의 trisomy/md 데이터를 기존 PPTX placeholder에 맞춰 변환

    Args:
        data_row: 리포트 데이터
        package_code: 패키지 코드 (예: "MD141")
    """
    default_result = "Low Risk"
    default_risk_after = data_row.get("Risk After", "")

    # 주요 트리소미 항목
    trisomy_items = [
        "T21", "T18", "T13", "XO", "XXX", "XXY", "XYY",
        "T9", "T16", "T22", "T1", "T2", "T3", "T4", "T5", "T6", "T7",
        "T8", "T10", "T11", "T12", "T14", "T15", "T17", "T19", "T20"
    ]

    # 위험도 데이터 준비
    trisomy_data = {}
    for entry in data_row.get("trisomy_result", []) + data_row.get("trisomy_others", []):
        if isinstance(entry, dict):
            item = entry.get("item", "").strip()
            # 표준 키는 "risk_after" (소문자). "Risk After"는 하위 호환성을 위해 처리
            risk_after_value = entry.get("risk_after") or entry.get("Risk After", default_risk_after)
            trisomy_data[item] = {
                "result": entry.get("result", "High Risk" if risk_after_value and risk_after_value != "-" else default_result),
                "risk_after": risk_after_value
            }

    # 주요 항목 처리
    for item in trisomy_items:
        result = trisomy_data.get(item, {}).get("result", default_result)
        risk_after = trisomy_data.get(item, {}).get("risk_after") or default_risk_after
        data_row[f"{item} Result"] = result
        # High Risk일 때 risk_after 값이 있으면 사용, 없으면 default_risk_after 사용
        if result.lower() == "high risk":
            data_row[f"{item} Risk After"] = risk_after if risk_after and risk_after != "-" else default_risk_after
        else:
            data_row[f"{item} Risk After"] = default_risk_after

    # Other Result 처리
    # trisomy_others에서 직접 risk_after 값을 가져와서 사용
    minor_trisomies = [
        "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8",
        "T10", "T11", "T12", "T14", "T15", "T17", "T19", "T20"
    ]
    data_row["Other Result"] = "Low Risk"
    data_row["Other Risk After"] = default_risk_after

    # trisomy_others에서 High Risk인 첫 번째 minor trisomy의 risk_after 사용
    for t in minor_trisomies:
        t_data = trisomy_data.get(t, {})
        if t_data.get("result", "").lower().strip() == "high risk":
            data_row["Other Result"] = "High Risk"
            # trisomy_data에서 직접 risk_after 값을 가져옴
            risk_after = t_data.get("risk_after") or default_risk_after
            data_row["Other Risk After"] = risk_after if risk_after and risk_after != "-" else default_risk_after
            break  # 첫 번째 High Risk 항목만 사용

    # MD 항목은 별도 처리 함수로 위임
    assign_md_results(data_row, package_code)

def process_trisomy_md_placeholders_old(data_row: Dict[str, Any]) -> None:
    """
    새 구조의 trisomy/md 데이터를 기존 PPTX placeholder에 맞춰 변환
    """
    default_result = "Low Risk"
    default_risk_after = data_row.get("Risk After", "")

    # 주요 트리소미 항목
    trisomy_items = [
        "T21", "T18", "T13", "XO", "XXX", "XXY", "XYY",
        "T9", "T16", "T22", "T1", "T2", "T3", "T4", "T5", "T6", "T7",
        "T8", "T10", "T11", "T12", "T14", "T15", "T17", "T19", "T20"
    ]

    # 위험도 데이터 준비
    trisomy_data = {}
    for entry in data_row.get("trisomy_result", []) + data_row.get("trisomy_others", []):
        if isinstance(entry, dict):
            item = entry.get("item", "").strip()
            trisomy_data[item] = {
                "result": entry.get("result", "High Risk" if entry.get("risk_after") else default_result),
                "risk_after": entry.get("risk_after", default_risk_after)
            }

    # 각 주요 항목 → <<Txx Result>>, <<Txx Risk After>>
    for item in trisomy_items:
        result = trisomy_data.get(item, {}).get("result", default_result)
        risk_after = trisomy_data.get(item, {}).get("risk_after", "")
        data_row[f"{item} Result"] = result
        data_row[f"{item} Risk After"] = risk_after if result.lower() == "high risk" else default_risk_after

    # Other Result 처리 (minor trisomy들 중 High Risk가 있으면 반영)
    minor_trisomies = [
        "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8",
        "T10", "T11", "T12", "T14", "T15", "T17", "T19", "T20"
    ]
    data_row["Other Result"] = "Low Risk"
    data_row["Other Risk After"] = default_risk_after
    for t in minor_trisomies:
        if data_row.get(f"{t} Result", "").lower().strip() == "high risk":
            data_row["Other Result"] = "High Risk"
            data_row["Other Risk After"] = data_row.get(f"{t} Risk After", "")
            break

    # MD 처리
    # md_result 처리: MD1~MD8 또는 "Other MD"
    for md_item in data_row.get("md_result", []):
        if md_item in ["MD1", "MD2", "MD3", "MD4", "MD5", "MD6", "MD7", "MD8"]:
            data_row[md_item] = "High Risk"
        elif md_item == "Other MD":
            data_row["Other MD"] = "High Risk"

    # md_others 처리: string 리스트, 모두 Low Risk로 설정
    for name in data_row.get("md_others", []):
        name = name.strip()

        # Alpha-thalassemia 특수 처리
        if name.startswith("Alpha-thalassemia"):
            data_row["Alpha-thalassemia"] = "Low Risk"
        else:
            data_row[name] = "Low Risk"


def populate_ppt(template_path: str, output_path: str, data_row: Dict[str, Any], report_lang: str) -> None:
    """PowerPoint 템플릿에 데이터 채우기 (새 구조 지원)"""
    try:
        prs = Presentation(template_path)

        for slide in prs.slides:
            for shape in slide.shapes:
                # 텍스트 프레임 처리
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        # 먼저 run 단위로 처리 (포맷팅 유지)
                        for run in paragraph.runs:
                            replace_placeholder_in_run(run, paragraph, data_row, report_lang)

                        # run에서 처리 못한 placeholder가 남아있는 경우에만 paragraph 단위로 처리
                        # (이미 처리된 경우 실행하지 않아 font style 보존)
                        if '<<' in paragraph.text and '>>' in paragraph.text:
                            replace_placeholder_in_paragraph(paragraph, data_row, report_lang)

                # 표 처리
                if shape.has_table:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            for paragraph in cell.text_frame.paragraphs:
                                # 먼저 run 단위로 처리
                                for run in paragraph.runs:
                                    replace_placeholder_in_run(run, paragraph, data_row, report_lang)

                                # run에서 처리 못한 placeholder가 남아있는 경우에만 paragraph 단위로 처리
                                # (이미 처리된 경우 실행하지 않아 font style 보존)
                                if '<<' in paragraph.text and '>>' in paragraph.text:
                                    replace_placeholder_in_paragraph(paragraph, data_row, report_lang)

        prs.save(output_path)
        logger.debug(f"PowerPoint saved: {output_path}")

    except Exception as e:
        logger.error(f"Error populating PowerPoint template: {e}")
        raise

def convert_to_pdf(input_pptx: str, output_dir: str) -> None:
    """PowerPoint을 PDF로 변환"""
    try:
        subprocess.run([
            "soffice", "--headless", "--convert-to", "pdf:impress_pdf_Export",
            "--outdir", output_dir, input_pptx
        ], check=True)
        logger.debug(f"PDF conversion completed: {input_pptx}")
    except subprocess.CalledProcessError as e:
        logger.error(f"PDF conversion failed: {e}")
        raise

def merge_pdfs(pdf_paths: List[str], output_path: str) -> None:
    """여러 PDF를 하나로 병합 (_CN.pdf가 맨 앞에 오도록)"""
    from pypdf import PdfReader
    try:
        def sort_key(filename: str):
            if "_CN.pdf" in filename.upper():
                return (0, filename)
            return (1, filename)

        sorted_paths = sorted(pdf_paths, key=sort_key)

        writer = PdfWriter()
        for pdf in sorted_paths:
            if os.path.exists(pdf):
                reader = PdfReader(pdf)
                for page in reader.pages:
                    writer.add_page(page)
            else:
                logger.warning(f"PDF file not found: {pdf}")

        with open(output_path, "wb") as f:
            writer.write(f)
        logger.info(f"PDFs merged successfully: {output_path}")

    except Exception as e:
        logger.error(f"Error merging PDFs: {e}")
        raise

def get_template_files(template_dir: str) -> Dict[str, str]:
    """템플릿 디렉토리에서 사용 가능한 템플릿 파일 목록 가져오기"""
    if not os.path.exists(template_dir):
        logger.error(f"Template directory not found: {template_dir}")
        return {}

    template_files = {
        os.path.splitext(f)[0]: f
        for f in os.listdir(template_dir) if f.endswith(".pptx")
    }

    logger.debug(f"Found {len(template_files)} template files")
    return template_files

def find_client_template_directory(template_dir: str, client_name: str) -> str:
    """
    클라이언트 이름으로 템플릿 디렉토리를 찾는 함수
    공백이 있는 디렉토리명도 처리
    """
    if not client_name:
        return template_dir

    # 1순위: 클라이언트 이름 그대로 시도
    direct_path = os.path.join(template_dir, client_name)
    if os.path.exists(direct_path):
        logger.info(f"Found exact match template directory: {direct_path}")
        return direct_path

    # 2순위: 디렉토리 목록에서 부분 매칭 시도
    if os.path.exists(template_dir):
        for dir_name in os.listdir(template_dir):
            dir_path = os.path.join(template_dir, dir_name)
            if os.path.isdir(dir_path):
                # 대소문자 무시하고 부분 매칭
                if client_name.lower() in dir_name.lower() or dir_name.lower() in client_name.lower():
                    logger.info(f"Found partial match template directory: {dir_path}")
                    return dir_path

    logger.warning(f"No template directory found for client '{client_name}', using default: {template_dir}")
    return template_dir

def assign_gender_text(row: Dict[str, Any]) -> None:
    """
    쌍둥이일 경우 성별 정보로 Y chromosome 텍스트 삽입
    """
    gender = str(row.get("Gender", "")).strip().lower()
    pregnancy_type = str(row.get("Pregnancy Type", "")).strip().lower()

    if pregnancy_type == "twins":
        if gender == "male":
            row["Gender"] = {"text": "Y Chromosome Detected", "font_size": 10}
        elif gender == "female":
            row["Gender"] = {"text": "Y Chromosome Not Detected", "font_size": 10}

def generate_from_json(report_json: Dict[str, Any], template_dir: str, output_dir: str) -> Dict[str, Any]:
    """
    JSON 데이터에서 PDF 리포트 생성
    Args:
        report_json: 리포트 데이터 (변수명 수정)
        template_dir: 템플릿 디렉토리 경로
        output_dir: 출력 디렉토리 경로
    Returns:
        생성된 파일 정보
    """
    logger.info(f"Generating report from JSON data")

    try:
        # 출력 디렉토리 생성
        os.makedirs(output_dir, exist_ok=True)

        # 클라이언트별 템플릿 디렉토리 찾기
        client_name = report_json.get("Client Name")
        logger.info(f"client_name : {client_name}")

        # find_client_template_directory 함수 사용
        actual_template_dir = find_client_template_directory(template_dir, client_name)
        logger.info(f"Using template directory: {actual_template_dir}")

        # 템플릿 파일 목록 가져오기 (실제 사용할 디렉토리에서)
        template_files = get_template_files(actual_template_dir)
        if not template_files:
            logger.warning(f"No template files found in {actual_template_dir}, trying default directory")
            # 클라이언트별 디렉토리에 템플릿이 없으면 기본 디렉토리 시도
            template_files = get_template_files(template_dir)
            actual_template_dir = template_dir

        if not template_files:
            raise FileNotFoundError(f"No template files found in {template_dir}")

        # 리포트 데이터 준비 (변수명 수정: json_data → report_json)
        row = report_json.copy()
        #logger.info(row)
        langs = row.get("Report Language", [])
        logger.info(f"Report Language : {langs}")

        # 언어 목록 정규화
        if isinstance(langs, str):
            langs = [langs]
        if not langs:
            langs = ["EN"]  # 기본값

        # MD 결과 할당 (새 구조에 맞게 수정)
        assign_gender_text(row)

        individual_pdfs = []
        report_id = str(row.get("Order ID", "Report")).strip().replace(" ", "_")
        logger.info(f"Report Id : {report_id}")
        sample_id = str(row.get("Sample ID", "None")).strip().replace(" ", "_")
        logger.info(f"Sample Id : {sample_id}")

        result = str(row.get("Result", "")).strip().lower()

        # 각 언어별로 리포트 생성
        for lang in [l.strip().upper() for l in langs]:
            try:
                # 템플릿 파일 찾기
                if result == "no call":
                    template_key = "No_Call"
                else:
                    template_key = row.get("TemplateKey")
                    if not template_key or not str(template_key).strip():
                        logger.error(f"TemplateKey is missing for order {row.get('Order ID')}")
                        raise ValueError(f"TemplateKey is required but missing for order {row.get('Order ID')}")
                    template_key = str(template_key).strip()
                    # There's space in package code
                    template_key = re.sub(r"\s+", "_", template_key)

                template_filename = template_files.get(f"{template_key}_{lang}")
                logger.info(f"Template '{template_filename}")

                if not template_filename:
                    logger.warning(f"Template '{template_key}_{lang}' not found, skipping")
                    continue

                # 파일 경로 설정 (실제 템플릿 디렉토리 사용)
                template_path = os.path.join(actual_template_dir, template_filename)
                pptx_filename = os.path.join(output_dir, f"{report_id}_{lang}.pptx")
                pdf_filename = os.path.join(output_dir, f"{report_id}_{lang}.pdf")

                # 새 구조 데이터를 기존 플레이스홀더 형태로 변환
                # Package 정보를 전달하여 MD141 패키지 전용 리스트 사용
                package_code = row.get("Package")
                process_trisomy_md_placeholders(row, package_code)

                # PowerPoint 생성
                populate_ppt(template_path, pptx_filename, row, lang)

                # PDF 변환
                convert_to_pdf(pptx_filename, output_dir)

                # 생성된 PDF 확인
                if os.path.exists(pdf_filename):
                    individual_pdfs.append(pdf_filename)
                    logger.info(f"Generated PDF: {pdf_filename}")
                else:
                    logger.warning(f"PDF not generated: {pdf_filename}")

                # 임시 PowerPoint 파일 삭제
                if os.path.exists(pptx_filename):
                    os.remove(pptx_filename)

            except Exception as e:
                logger.error(f"Error generating report for language {lang}: {e}")
                continue

        # PDF 병합 (1개면 병합 없이 복사, 2개 이상이면 병합)
        merged_pdf = None
        if individual_pdfs:
            merged_pdf_path = os.path.join(output_dir, f"{report_id}_{sample_id}.pdf" if sample_id != "None" else f"{report_id}.pdf")

            if len(individual_pdfs) == 1:
                # 1개뿐이면 복사만 하기
                import shutil
                shutil.copy2(individual_pdfs[0], merged_pdf_path)
                logger.info(f"Single PDF copied to merged file: {merged_pdf_path}")
                merged_pdf = merged_pdf_path
            else:
                # 2개 이상이면 병합
                merge_pdfs(individual_pdfs, merged_pdf_path)
                logger.info(f"Multiple PDFs merged: {len(individual_pdfs)} files")

                if os.path.exists(merged_pdf_path):
                    merged_pdf = merged_pdf_path
                else:
                    logger.error("Merged PDF file was not created")
                    merged_pdf = None

        result = {
            "merged": merged_pdf,
            "individual": individual_pdfs
        }

        logger.info(f"Report generation completed. Generated {len(individual_pdfs)} individual PDFs")
        return result

    except Exception as e:
        logger.error(f"Error generating report: {e}")
        raise

async def upload_pdf_report(order_id: str, pdf_path: str, *, signed: bool = False) -> bool:
    """생성된 PDF 리포트를 Platform에 업로드"""
    kind = "SIGNED" if signed else "NORMAL"
    logger.info(f"Uploading {kind} PDF report for order: {order_id}")
    logger.info(f"PDF path: {pdf_path}")

    try:
        from .notifier import upload_pdf_report as _notifier_upload
        return await _notifier_upload(order_id, pdf_path, signed=signed)
    except Exception as e:
        logger.error(f"Error uploading {kind} PDF for {order_id}: {str(e)}")
        return False

# 메인 워크플로우 함수
async def generate_and_upload_report(order_id: str, review_json: Dict[str, Any],
                                   template_dir: str, output_dir: str) -> bool:
    """
    전체 리포트 생성 및 업로드 워크플로우
    """
    logger.info(f"Starting report generation workflow for order: {order_id}")

    try:
        # 1. 리포트 JSON 생성
        report_json = await make_report_json(order_id, review_json)

        logger.info(report_json)

        # 2. PDF 리포트 생성
        result = generate_from_json(report_json, template_dir, output_dir)

        # 3. 병합된 PDF 업로드
        if result["merged"]:
            upload_success = await upload_pdf_report(order_id, result["merged"], signed=False)
            if upload_success:
                logger.info(f"Report workflow completed successfully for {order_id}")
                return True
            else:
                logger.error(f"Failed to upload report for {order_id}")
                return False
        else:
            logger.error(f"No merged PDF generated for {order_id}")
            return False

    except Exception as e:
        logger.error(f"Error in report generation workflow for {order_id}: {e}")
        return False
