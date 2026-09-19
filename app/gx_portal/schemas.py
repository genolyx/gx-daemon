"""GET /v1/order-schema payloads. Clinical fields only — FASTQ stays on sample."""

from typing import Any, Dict, List, Optional

from .codes import DAEMON_TO_GX, resolve_daemon_service, to_gx_code

SCHEMA_VERSION = "2026-09-18"


def _f(
    key: str,
    label: str,
    ftype: str,
    order: int,
    required: bool = False,
    enum_values: Optional[List[str]] = None,
    enum_labels: Optional[List[str]] = None,
    default: Any = None,
    validation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "fieldKey": key,
        "fieldLabel": label,
        "fieldType": ftype,
        "isRequired": required,
        "order": order,
        "defaultValue": default,
        "validation": validation,
        "enumValues": enum_values or [],
        "enumLabels": enum_labels or [],
    }
    return item


def _exome_fields(*, health: bool = False) -> List[Dict[str, Any]]:
    fields = [
        _f("patientName", "Patient Name", "string", 1, True, validation={"maxLength": 80}),
        _f("patientBirth", "Date of Birth", "date", 2, True),
        _f(
            "patientGender",
            "Gender",
            "enum",
            3,
            True,
            ["FEMALE", "MALE", "OTHER"],
            ["Female", "Male", "Other"],
        ),
        _f("affected", "Affected", "enum", 4, True, ["YES", "NO"], ["Yes", "No"]),
        _f("clinicalInformation", "Clinical information", "string", 5, validation={"maxLength": 4000}),
        _f("wesPanelId", "Primary (interpretation) panel", "string", 6, True, validation={"maxLength": 80}),
        _f(
            "reportLanguage",
            "Report Language",
            "enum",
            7,
            True,
            ["EN", "CN", "KO", "EN_CN"],
            ["English", "Chinese", "Korean", "English + Chinese"],
        ),
        _f(
            "reportType",
            "Report Type",
            "enum",
            8,
            True,
            ["PRINTOUT", "EMAIL", "PORTAL"],
            ["Printout", "Email", "Portal"],
        ),
        _f(
            "sampleSpecimenType",
            "Sample Specimen Type",
            "enum",
            9,
            False,
            ["BLOOD", "SALIVA", "OTHER"],
            ["Blood", "Saliva", "Other"],
            default="BLOOD",
        ),
        _f("includePgx", "Include PGx on customer PDF", "boolean", 10, default=True),
    ]
    if health:
        fields.append(
            _f("includeApoePgx", "APOE genotype tag SNPs", "boolean", 11, default=False)
        )
    else:
        fields.extend(
            [
                _f(
                    "reportMode",
                    "Carrier report mode",
                    "enum",
                    11,
                    False,
                    ["SINGLE", "COUPLES"],
                    ["Single report", "Couples report"],
                    default="SINGLE",
                ),
                _f("partnerOrderId", "Partner Order ID", "string", 12, validation={"maxLength": 64}),
            ]
        )
    return fields


def _sgnipt_fields() -> List[Dict[str, Any]]:
    return [
        _f("patientName", "Patient Name", "string", 1, True, validation={"maxLength": 80}),
        _f("patientBirth", "Date of Birth", "date", 2, True),
        _f(
            "patientGender",
            "Gender",
            "enum",
            3,
            True,
            ["FEMALE", "MALE", "OTHER"],
            ["Female", "Male", "Other"],
            default="FEMALE",
        ),
        _f("gestationalAgeWeeks", "GA Weeks", "number", 4, True, validation={"min": 0, "max": 45}),
        _f("gestationalAgeDays", "GA Days", "number", 5, True, validation={"min": 0, "max": 6}),
        _f(
            "pregnancyType",
            "Pregnancy Type",
            "enum",
            6,
            True,
            ["SINGLETON", "TWIN", "MULTIPLE"],
            ["Singleton", "Twin", "Multiple"],
        ),
        _f("estimatedDeliveryDate", "Estimated Delivery Date", "date", 7, True),
        _f("height", "Height (cm)", "number", 8, validation={"min": 0, "max": 300}),
        _f("weight", "Weight (kg)", "number", 9, validation={"min": 0, "max": 300}),
        _f(
            "packageCode",
            "Package Code",
            "enum",
            10,
            True,
            ["BASIC", "STANDARD", "ULTIMATE_PLUS", "OTHER"],
            ["Basic", "Standard", "Ultimate Plus", "Other"],
        ),
        _f(
            "reportLanguage",
            "Report Language",
            "enum",
            11,
            True,
            ["EN", "KO", "CN", "ID", "OTHER"],
            ["English", "Korean", "Chinese", "Indonesian", "Other"],
        ),
        _f(
            "reportType",
            "Report Type",
            "enum",
            12,
            True,
            ["PRINTOUT", "EMAIL", "PORTAL"],
            ["Printout", "Email", "Portal"],
        ),
        _f(
            "indication",
            "Indication for Testing",
            "enum",
            13,
            False,
            ["ADVANCED_MATERNAL_AGE", "ABNORMAL_ULTRASOUND", "OTHER"],
            ["Advanced maternal age", "Abnormal ultrasound", "Other"],
        ),
        _f(
            "sampleSpecimenType",
            "Sample Specimen Type",
            "enum",
            14,
            False,
            ["BLOOD", "PLASMA", "OTHER"],
            ["Blood", "Plasma", "Other"],
            default="BLOOD",
        ),
        _f("controlSample", "Control Sample", "enum", 15, True, ["NO", "YES"], ["No", "Yes"], default="NO"),
        _f("trfConsent", "TRF Consent", "enum", 16, True, ["YES", "NO"], ["Yes", "No"], default="YES"),
        _f("showFetalGender", "Show Fetal Gender", "enum", 17, True, ["YES", "NO"], ["Yes", "No"], default="YES"),
        _f("resample", "Resample", "enum", 18, True, ["NO", "YES"], ["No", "Yes"], default="NO"),
    ]


_FIELDS = {
    "carrier_screening": _exome_fields(health=False),
    "whole_exome": _exome_fields(health=False),
    "health_screening": _exome_fields(health=True),
    "sgnipt": _sgnipt_fields(),
}


def get_order_schema(service_code: str) -> Optional[Dict[str, Any]]:
    daemon = resolve_daemon_service(service_code)
    if not daemon or daemon not in _FIELDS:
        return None
    return {
        "service_code": to_gx_code(daemon),
        "version": SCHEMA_VERSION,
        "fields": _FIELDS[daemon],
    }


def list_schema_codes() -> List[str]:
    return [DAEMON_TO_GX[c] for c in _FIELDS]
