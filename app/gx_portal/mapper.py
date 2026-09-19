"""Map GX service_data + organization + sample → daemon job.params."""

from typing import Any, Dict

_GENDER = {"FEMALE": "Female", "MALE": "Male", "OTHER": "Other"}
_YN = {"YES": "Yes", "NO": "No"}
_SPECIMEN = {
    "BLOOD": "Blood",
    "SALIVA": "Saliva",
    "PLASMA": "Plasma",
    "OTHER": "Other",
}
_REPORT_TYPE = {"PRINTOUT": "Printout", "EMAIL": "Email", "PORTAL": "Portal"}
_PREGNANCY = {"SINGLETON": "Singleton", "TWIN": "Twin", "MULTIPLE": "Multiple"}
_PACKAGE = {
    "BASIC": "Basic",
    "STANDARD": "Standard",
    "ULTIMATE_PLUS": "Ultimate_Plus",
    "OTHER": "Other",
}
_INDICATION = {
    "ADVANCED_MATERNAL_AGE": "Advanced_maternal_age",
    "ABNORMAL_ULTRASOUND": "Abnormal_ultrasound",
    "OTHER": "Other",
}


def _s(data: Dict[str, Any], key: str, default: str = "") -> str:
    v = data.get(key)
    if v is None:
        return default
    return str(v).strip()


def _enum(data: Dict[str, Any], key: str, table: Dict[str, str], default: str = "") -> str:
    raw = _s(data, key).upper().replace("-", "_").replace(" ", "_")
    return table.get(raw, default or _s(data, key))


def _bool(data: Dict[str, Any], key: str, default: bool = False) -> bool:
    v = data.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes")


def _report_language(data: Dict[str, Any]) -> str:
    raw = _s(data, "reportLanguage").upper().replace("-", "_")
    if raw == "EN_CN":
        return "EN,CN"
    return raw or "EN"


def map_daemon_params(
    service_code: str,
    service_data: Dict[str, Any],
    organization: Dict[str, Any],
    sample: Dict[str, Any],
    gx_meta: Dict[str, Any],
) -> Dict[str, Any]:
    org = organization or {}
    samp = sample or {}
    data = service_data or {}
    hospital = _s(org, "hospital_name") or _s(org, "name")
    doctor = _s(org, "doctor")
    sample_id = _s(samp, "sample_id")
    mrn = _s(samp, "medical_record_id")
    collected = _s(samp, "sample_collected_at")

    if service_code == "sgnipt":
        nipt = {
            "patient_name": _s(data, "patientName"),
            "patient_birth": _s(data, "patientBirth"),
            "patient_gender": _enum(data, "patientGender", _GENDER, "Female"),
            "gestational_age_weeks": data.get("gestationalAgeWeeks"),
            "gestational_age_days": data.get("gestationalAgeDays"),
            "pregnancy_type": _enum(data, "pregnancyType", _PREGNANCY),
            "estimated_delivery_date": _s(data, "estimatedDeliveryDate"),
            "height_cm": data.get("height"),
            "weight_kg": data.get("weight"),
            "hospital_name": hospital,
            "doctor": doctor,
            "medical_record_id": mrn,
            "sample_id": sample_id,
            "sample_collection_date": collected,
            "indication": _enum(data, "indication", _INDICATION),
            "package_code": _enum(data, "packageCode", _PACKAGE),
            "report_language": _report_language(data),
            "report_type": _enum(data, "reportType", _REPORT_TYPE, "Portal"),
            "sample_specimen_type": _enum(data, "sampleSpecimenType", _SPECIMEN, "Blood"),
            "control_sample": _enum(data, "controlSample", _YN, "No"),
            "trf_consent": _enum(data, "trfConsent", _YN, "Yes"),
            "show_fetal_gender": _enum(data, "showFetalGender", _YN, "Yes"),
            "resample": _enum(data, "resample", _YN, "No"),
        }
        return {"nipt": nipt, "_gx": gx_meta}

    wes = _s(data, "wesPanelId")
    report_mode = _s(data, "reportMode").upper()
    carrier = {
        "test_category": "standard_carrier",
        "patient_name": _s(data, "patientName"),
        "patient_birth": _s(data, "patientBirth"),
        "patient_gender": _enum(data, "patientGender", _GENDER, "Female"),
        "affected": _enum(data, "affected", _YN, "No"),
        "clinical_information": _s(data, "clinicalInformation"),
        "hospital_name": hospital,
        "doctor": doctor,
        "medical_record_id": mrn,
        "sample_id": sample_id,
        "sample_collection_date": collected,
        "report_language": _report_language(data),
        "report_type": _enum(data, "reportType", _REPORT_TYPE, "Portal"),
        "sample_specimen_type": _enum(data, "sampleSpecimenType", _SPECIMEN, "Blood"),
        "wes_panel_id": wes,
        "include_pgx": _bool(data, "includePgx", True),
        "include_apoe_pgx": _bool(data, "includeApoePgx", False),
        "report_mode": "couples" if report_mode == "COUPLES" else "single",
        "partner_order_id": _s(data, "partnerOrderId"),
        "capture_panel_id": "twist-exome2",
        "reuse_prior_pipeline_outputs": False,
    }
    params: Dict[str, Any] = {
        "carrier": carrier,
        "wes_panel_id": wes,
        "panel_filter_after_analysis": True,
        "_gx": gx_meta,
        "_gx_submit": True,
    }
    if not wes:
        params["_platform_submit"] = True
        carrier["_platform_submit"] = True
    return params
