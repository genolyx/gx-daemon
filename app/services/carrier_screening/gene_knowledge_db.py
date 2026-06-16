"""
SQLite gene knowledge cache (compatible with genetic_reporter_lookup ``gene_data``).

- ``init_gene_knowledge_database`` — create ``gene_data`` if missing.
- ``diseases_from_gene_knowledge_sqlite`` — read disorder / OMIM / inheritance for annotator.
- ``fetch_gene_knowledge_via_gemini`` — same flow as Sam's app (Search + JSON extract).
- ``get_or_fetch_gene_diseases`` — DB first; optional Gemini + upsert on miss.
- ``enrich_confirmed_variants_for_report`` — SQLite/Gemini merge for **Generate Report** only.

Prefer batch prefetch via ``scripts/populate_gene_knowledge_db.py`` to avoid Gemini latency on report.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def init_gene_knowledge_database(db_path: str) -> None:
    """
    Create ``gene_data`` table (same columns as genetic_reporter_lookup ``init_db``).
    Parent directories are created as needed.
    """
    if not db_path:
        return
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gene_data (
                gene_symbol TEXT PRIMARY KEY,
                function_summary TEXT,
                disease_association TEXT,
                omim_number TEXT,
                inheritance TEXT,
                disorder TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS variant_knowledge (
                variant_key TEXT PRIMARY KEY,
                gene_symbol TEXT NOT NULL,
                hgvsc TEXT,
                hgvsp TEXT,
                variant_notes TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()


def _row_to_disease_list(row: sqlite3.Row) -> List[Dict[str, Any]]:
    name = (row["disorder"] or "").strip()
    if not name:
        return []
    omim = (row["omim_number"] or "").strip().replace("OMIM:", "")
    return [
        {
            "name": name,
            "disease_name": name,
            "omim_id": omim,
            "inheritance": (row["inheritance"] or "").strip(),
            "source": "gene_knowledge_db",
        }
    ]


def read_gene_knowledge_full_row(gene: str, db_path: str) -> Optional[Dict[str, str]]:
    """
    Full ``gene_data`` row (SQLite / Gemini cache). Used by portal Gene database + report defaults.
    """
    if not gene or not db_path or not os.path.isfile(db_path):
        return None
    g = gene.strip().upper()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT gene_symbol, function_summary, disease_association,
                       omim_number, inheritance, disorder
                FROM gene_data WHERE gene_symbol = ?
                """,
                (g,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.debug("gene_knowledge_db full read failed: %s", e)
        return None
    if not row:
        return None
    omim = (row["omim_number"] or "").strip().replace("OMIM:", "")
    return {
        "gene_symbol": (row["gene_symbol"] or g).strip().upper(),
        "function_summary": (row["function_summary"] or "").strip(),
        "disease_association": (row["disease_association"] or "").strip(),
        "omim_number": omim,
        "inheritance": (row["inheritance"] or "").strip(),
        "disorder": (row["disorder"] or "").strip(),
    }


def gene_knowledge_row_is_empty(row: Optional[Dict[str, str]]) -> bool:
    """True if cache miss or no usable text for display / report defaults."""
    if not row:
        return True
    keys = ("disorder", "function_summary", "disease_association", "omim_number", "inheritance")
    return not any((row.get(k) or "").strip() for k in keys)


def make_variant_key(gene: str, hgvsc: str = "", hgvsp: str = "") -> str:
    """
    Stable primary key for variant-level rows across orders (same gene + HGVS reuses one row).
    Prefer cDNA (hgvsc); else protein (hgvsp); else gene-only suffix (rare).
    """
    g = (gene or "").strip().upper()
    hc = (hgvsc or "").strip()
    hp = (hgvsp or "").strip()
    if hc:
        return f"{g}|{hc}"
    if hp:
        return f"{g}|{hp}"
    return f"{g}|"


def read_variant_knowledge_row(variant_key: str, db_path: str) -> Optional[Dict[str, str]]:
    """Single ``variant_knowledge`` row (per-variant notes, separate from shared ``gene_data``)."""
    if not variant_key or not db_path or not os.path.isfile(db_path):
        return None
    init_gene_knowledge_database(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT variant_key, gene_symbol, hgvsc, hgvsp, variant_notes, updated_at
                FROM variant_knowledge WHERE variant_key = ?
                """,
                (variant_key,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.debug("variant_knowledge read failed: %s", e)
        return None
    if not row:
        return None
    return {
        "variant_key": (row["variant_key"] or "").strip(),
        "gene_symbol": (row["gene_symbol"] or "").strip().upper(),
        "hgvsc": (row["hgvsc"] or "").strip(),
        "hgvsp": (row["hgvsp"] or "").strip(),
        "variant_notes": (row["variant_notes"] or "").strip(),
        "updated_at": (row["updated_at"] or "").strip(),
    }


def load_variant_knowledge_for_keys(db_path: str, keys: List[str]) -> Dict[str, Dict[str, str]]:
    """Batch-read ``variant_knowledge`` rows for many keys (skips missing)."""
    keys = [k for k in keys if k]
    if not db_path or not os.path.isfile(db_path) or not keys:
        return {}
    init_gene_knowledge_database(db_path)
    out: Dict[str, Dict[str, str]] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(keys))
            cur = conn.execute(
                f"""
                SELECT variant_key, gene_symbol, hgvsc, hgvsp, variant_notes, updated_at
                FROM variant_knowledge WHERE variant_key IN ({placeholders})
                """,
                keys,
            )
            for row in cur:
                vk = (row["variant_key"] or "").strip()
                if not vk:
                    continue
                out[vk] = {
                    "variant_key": vk,
                    "gene_symbol": (row["gene_symbol"] or "").strip().upper(),
                    "hgvsc": (row["hgvsc"] or "").strip(),
                    "hgvsp": (row["hgvsp"] or "").strip(),
                    "variant_notes": (row["variant_notes"] or "").strip(),
                    "updated_at": (row["updated_at"] or "").strip(),
                }
    except Exception as e:
        logger.debug("variant_knowledge batch read failed: %s", e)
    return out


def upsert_variant_knowledge(db_path: str, row: Dict[str, str]) -> None:
    """INSERT OR REPLACE one ``variant_knowledge`` row (variant-specific text; gene-level stays in ``gene_data``)."""
    init_gene_knowledge_database(db_path)
    vk = (row.get("variant_key") or "").strip()
    if not vk:
        return
    gene_symbol = (row.get("gene_symbol") or "").strip().upper()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO variant_knowledge
            (variant_key, gene_symbol, hgvsc, hgvsp, variant_notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                vk,
                gene_symbol,
                (row.get("hgvsc") or "").strip(),
                (row.get("hgvsp") or "").strip(),
                (row.get("variant_notes") or "").strip(),
                ts,
            ),
        )
        conn.commit()


def diseases_from_gene_knowledge_sqlite(gene: str, db_path: str) -> List[Dict[str, Any]]:
    """Read ``gene_data`` row; shape matches ``VariantAnnotator`` ``diseases`` entries."""
    if not gene or not db_path or not os.path.isfile(db_path):
        return []
    g = gene.strip().upper()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT disorder, omim_number, inheritance FROM gene_data WHERE gene_symbol = ?",
                (g,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.debug("gene_knowledge_db read failed: %s", e)
        return []
    if not row:
        return []
    return _row_to_disease_list(row)


def _gene_knowledge_row_from_parsed(parsed: Any, gene_symbol: str) -> Optional[Dict[str, str]]:
    """Build flat row if any clinical field is non-empty (do not require disorder_name alone)."""
    disorder = (getattr(parsed, "disorder_name", None) or "").strip()
    fs = (getattr(parsed, "function_summary", None) or "").strip()
    da = (getattr(parsed, "disease_association", None) or "").strip()
    omim = (getattr(parsed, "omim_number", None) or "").strip().replace("OMIM:", "")
    inh = (getattr(parsed, "inheritance", None) or "").strip()
    if not disorder and not fs and not da and not omim and not inh:
        return None
    return {
        "gene_symbol": gene_symbol,
        "disorder": disorder,
        "omim_number": omim,
        "inheritance": inh,
        "disease_association": da,
        "function_summary": fs,
    }


def _gene_knowledge_json_prompt(gene_symbol: str) -> Tuple[str, str]:
    """공통 JSON 프롬프트 (Gemini fallback / Ollama / OpenAI-compat 모두 사용)."""
    system = (
        "You are a genomics expert. Return ONLY a valid JSON object with exactly these keys: "
        "gene_symbol, omim_number, function_summary, disease_association, inheritance, disorder_name. "
        "Keep each value under 2 sentences. No prose, no markdown, no extra keys."
    )
    user = (
        f"Gene: {gene_symbol}\n"
        "Return JSON only:\n"
        '{"gene_symbol":"","omim_number":"","function_summary":"","disease_association":"","inheritance":"","disorder_name":""}'
    )
    return system, user


def fetch_gene_knowledge_via_openai_compat(
    gene: str,
    model: str,
    base_url: str = "http://host.docker.internal:11434/v1",
    api_key: str = "ollama",
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Ollama / vLLM / LM Studio 등 OpenAI-compatible LLM을 통한 gene knowledge fetch.
    Google Search 없이 순수 JSON 프롬프트만 사용합니다.
    """
    import asyncio
    gene_symbol = (gene or "").strip().upper()
    if not gene_symbol:
        return None, "missing gene"
    try:
        from app.services._resolve_ai import call_ai_json
    except ImportError:
        try:
            from services._resolve_ai import call_ai_json  # type: ignore
        except ImportError:
            return None, "call_ai_json not available"

    system, user = _gene_knowledge_json_prompt(gene_symbol)
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            call_ai_json(
                provider="ollama",
                model=model,
                api_key=api_key,
                system_prompt=system,
                user_content=user,
                temperature=0.1,
                max_tokens=400,
                base_url=base_url,
            )
        )
        loop.close()
    except Exception as e:
        return None, str(e)

    if not result:
        return None, "No response from LLM"

    flat = {
        "gene_symbol": result.get("gene_symbol") or gene_symbol,
        "omim_number": result.get("omim_number") or "",
        "function_summary": result.get("function_summary") or "",
        "disease_association": result.get("disease_association") or "",
        "inheritance": result.get("inheritance") or "",
        "disorder": result.get("disorder_name") or result.get("disorder") or "",
    }
    return flat, None


def fetch_gene_knowledge_via_gemini(
    gene: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Gemini + optional Google Search, then structured JSON (Sam pipeline).
    Returns (flat dict or None, error message or None).

    Tries Search+extract first; if that yields nothing or errors, retries with a single JSON prompt
    (no Search tool) so keys/APIs without Search still work.
    """
    if not api_key or not (gene or "").strip():
        return None, "missing API key or gene"
    try:
        from google import genai
        from pydantic import BaseModel, Field
    except ImportError as e:
        logger.warning("google-genai or pydantic missing; cannot run Gemini gene lookup")
        return None, str(e)

    class GeneKnowledge(BaseModel):
        omim_number: str = Field(default="")
        function_summary: str = Field(default="")
        disease_association: str = Field(default="")
        inheritance: str = Field(default="")
        disorder_name: str = Field(default="")

    client = genai.Client(api_key=api_key)
    gene_symbol = gene.strip().upper()
    errors: List[str] = []

    # (1) Search + extract (same as genetic_reporter_lookup)
    try:
        search_prompt = (
            f"Using Google Search, find the latest technical details and OMIM data for the gene {gene_symbol}. "
            "Prioritize sources: OMIM, GeneReviews, UniProt, GTEx. Summarize the findings comprehensively. "
            "Also identify the specific name of the primary disorder associated with this gene."
        )
        search_response = client.models.generate_content(
            model=model,
            contents=search_prompt,
            config={"tools": [{"google_search": {}}]},
        )
        found_text = search_response.text or ""

        extraction_prompt = f"""Extract the following details from the text below into the specified JSON format.
TEXT: {found_text}

Field notes (align with genetic_reporter_lookup gene_data usage):
- 'disorder_name': Specific name of the primary disorder associated with this gene.
- 'disease_association': 1–3 sentences on how pathogenic variants in this gene relate to that disorder (clinical/genetic mechanism), not just repeating the disorder name.
- 'function_summary': 1–3 sentences on normal gene product function / pathway role.
- 'inheritance': Pattern if known (e.g. AR, AD, XL).
- 'omim_number': Digits only when cited in the text.
"""
        response = client.models.generate_content(
            model=model,
            contents=extraction_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": GeneKnowledge,
            },
        )
        parsed = response.parsed
        if parsed:
            row = _gene_knowledge_row_from_parsed(parsed, gene_symbol)
            if row:
                return row, None
        errors.append("search path returned empty structured fields")
    except Exception as e:
        err = str(e)
        errors.append(f"search path: {err}")
        logger.warning("fetch_gene_knowledge_via_gemini search path failed for %s: %s", gene_symbol, e)

    # (2) Fallback: structured JSON without Search (API key / model only)
    try:
        fallback_prompt = (
            f"You are a clinical genetics assistant. For gene {gene_symbol}, provide accurate structured fields: "
            "disorder_name (primary disease name), omim_number (digits only if known), inheritance (e.g. AR/AD/XL), "
            "function_summary (1–3 sentences on gene function), disease_association (1–3 sentences linking gene to disease). "
            "Use established medical knowledge."
        )
        response = client.models.generate_content(
            model=model,
            contents=fallback_prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": GeneKnowledge,
            },
        )
        parsed = response.parsed
        if parsed:
            row = _gene_knowledge_row_from_parsed(parsed, gene_symbol)
            if row:
                return row, None
        errors.append("fallback JSON returned empty fields")
    except Exception as e:
        errors.append(f"fallback: {e}")
        logger.warning("fetch_gene_knowledge_via_gemini fallback failed for %s: %s", gene_symbol, e)

    merged = "; ".join(errors) if errors else "unknown Gemini failure"
    if "API_KEY_INVALID" in merged or "API Key not found" in merged:
        merged += (
            " Hint: use a current key from https://aistudio.google.com/apikey "
            "(or Google Cloud → APIs & Services → Credentials). For Docker/server calls, "
            "do not restrict the key to HTTP referrers only."
        )
    return None, merged


def upsert_gene_data(db_path: str, row: Dict[str, str]) -> None:
    """INSERT OR REPLACE full ``gene_data`` row."""
    init_gene_knowledge_database(db_path)
    gene_symbol = (row.get("gene_symbol") or "").strip().upper()
    if not gene_symbol:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO gene_data
            (gene_symbol, function_summary, disease_association, omim_number, inheritance, disorder)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                gene_symbol,
                row.get("function_summary") or "",
                row.get("disease_association") or "",
                row.get("omim_number") or "",
                row.get("inheritance") or "",
                row.get("disorder") or "",
            ),
        )
        conn.commit()


def ensure_gene_knowledge_full_text(
    gene: str,
    db_path: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
    *,
    allow_gemini: bool = True,
) -> Optional[Dict[str, str]]:
    """
    Return cached ``gene_data`` row. If ``function_summary`` or ``disease_association`` is empty
    and Gemini is allowed, call Gemini and upsert (refreshes narrative fields even when only
    disorder metadata existed from a partial insert).
    """
    init_gene_knowledge_database(db_path)
    row = read_gene_knowledge_full_row(gene, db_path)
    if row:
        fs = (row.get("function_summary") or "").strip()
        da = (row.get("disease_association") or "").strip()
        if fs and da:
            return row
    if not allow_gemini or not (api_key or "").strip():
        return row
    flat, _err = fetch_gene_knowledge_via_gemini(gene, api_key, model=model)
    if not flat:
        return row
    upsert_gene_data(
        db_path,
        {
            "gene_symbol": flat["gene_symbol"],
            "function_summary": flat.get("function_summary") or "",
            "disease_association": flat.get("disease_association") or "",
            "omim_number": flat.get("omim_number") or "",
            "inheritance": flat.get("inheritance") or "",
            "disorder": flat.get("disorder") or "",
        },
    )
    return read_gene_knowledge_full_row(gene, db_path)


def refresh_gene_knowledge_from_gemini(
    gene: str,
    db_path: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Always run Gemini + upsert into ``gene_data`` (portal “new search / write-up” for one gene).
    On API failure, returns (cached row or None, error message).
    """
    g = (gene or "").strip().upper()
    if not g or not db_path:
        return None, "missing gene or db path"
    init_gene_knowledge_database(db_path)
    if not (api_key or "").strip():
        return read_gene_knowledge_full_row(g, db_path), "GEMINI_API_KEY not set"
    flat, err = fetch_gene_knowledge_via_gemini(g, api_key, model=model)
    if not flat:
        return read_gene_knowledge_full_row(g, db_path), err
    upsert_gene_data(
        db_path,
        {
            "gene_symbol": flat["gene_symbol"],
            "function_summary": flat.get("function_summary") or "",
            "disease_association": flat.get("disease_association") or "",
            "omim_number": flat.get("omim_number") or "",
            "inheritance": flat.get("inheritance") or "",
            "disorder": flat.get("disorder") or "",
        },
    )
    return read_gene_knowledge_full_row(g, db_path), None


def fetch_gene_knowledge_from_vcf_files(
    gene: str,
    variants: List[Dict],
    hgmd_vcf: str = "",
    clinvar_vcf: str = "",
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    HGMD + ClinVar VCF 파일에서 유전자 지식을 구성합니다 (AI 불필요).
    해당 유전자의 변이 목록을 받아 각 파일에서 조회하고 유전자 수준으로 집계합니다.

    Args:
        gene:       gene symbol (e.g. "FGFR3")
        variants:   list of {"chrom", "pos", "ref", "alt", ...} dicts from result.json
        hgmd_vcf:   path to tabix-indexed HGMD VCF (.vcf.gz)
        clinvar_vcf: path to tabix-indexed ClinVar VCF (.vcf.gz)
    """
    from .annotator import ClinVarAnnotator, HGMDAnnotator

    gene_sym = (gene or "").strip().upper()
    if not gene_sym:
        return None, "missing gene"
    if not variants:
        return None, "no variants provided"

    hgmd_ann     = HGMDAnnotator(hgmd_vcf)     if hgmd_vcf     and os.path.isfile(hgmd_vcf)     else None
    clinvar_ann  = ClinVarAnnotator(clinvar_vcf) if clinvar_vcf and os.path.isfile(clinvar_vcf) else None

    if not hgmd_ann and not clinvar_ann:
        return None, "no annotation files available (set HGMD_VCF and/or CLINVAR_VCF)"

    hgmd_hits:    List[Dict] = []
    clinvar_hits: List[Dict] = []

    for v in variants:
        chrom = str(v.get("chrom") or "")
        try:
            pos = int(v.get("pos") or 0)
        except (TypeError, ValueError):
            continue
        ref = str(v.get("ref") or "")
        alt = str(v.get("alt") or "")
        if not (chrom and pos and ref and alt):
            continue

        if hgmd_ann:
            hr = hgmd_ann.lookup(chrom, pos, ref, alt)
            if hr:
                hgmd_hits.append(hr)

        if clinvar_ann:
            cr = clinvar_ann.lookup(chrom, pos, ref, alt)
            if cr:
                clinvar_hits.append(cr)

    # ── Aggregate HGMD ──────────────────────────────────────────
    dm_hits  = [h for h in hgmd_hits if h.get("hgmd_class") in ("DM", "DM?")]
    all_hgmd = hgmd_hits

    def _uniq(seq):
        seen = set()
        return [x for x in seq if x and x not in seen and not seen.add(x)]

    diseases_hgmd = _uniq([
        (h.get("hgmd_disease") or "").replace("_", " ")
        for h in dm_hits
    ])
    pmids_hgmd = _uniq([h.get("hgmd_pmid") or "" for h in dm_hits])

    # ── Aggregate ClinVar ────────────────────────────────────────
    plp_hits = [
        c for c in clinvar_hits
        if "pathogenic" in (c.get("clnsig_primary") or "").lower()
    ]
    conditions_cv = _uniq([
        (c.get("clndn") or "").replace("_", " ")
        for c in plp_hits
        if c.get("clndn") and c.get("clndn") not in ("not_provided", "not_specified")
    ])
    cv_stars = max((int(c.get("stars") or 0) for c in plp_hits), default=0)

    # ── Build gene knowledge fields ──────────────────────────────
    all_diseases = _uniq(diseases_hgmd + [d for d in conditions_cv if d not in diseases_hgmd])
    disorder = ", ".join(all_diseases[:3]) if all_diseases else ""

    evidence_parts: List[str] = []
    if dm_hits:
        evidence_parts.append(
            f"HGMD: {len(dm_hits)} disease-causing variant(s) "
            f"(DM: {sum(1 for h in dm_hits if h.get('hgmd_class')=='DM')}, "
            f"DM?: {sum(1 for h in dm_hits if h.get('hgmd_class')=='DM?')})"
        )
    if plp_hits:
        star_str = f" ★{'★' * (cv_stars - 1)}" if cv_stars else ""
        evidence_parts.append(
            f"ClinVar: {len(plp_hits)} P/LP variant(s){star_str}"
        )
    disease_association = "; ".join(evidence_parts) if evidence_parts else (
        "No pathogenic entries found in HGMD or ClinVar for the observed variants."
    )

    # function_summary: structured summary of known associations
    fs_parts: List[str] = []
    if all_diseases:
        fs_parts.append(f"Associated with: {', '.join(all_diseases[:5])}.")
    if pmids_hgmd:
        fs_parts.append(f"Key HGMD references (PMID): {', '.join(pmids_hgmd[:5])}.")
    function_summary = " ".join(fs_parts) if fs_parts else ""

    # OMIM / inheritance — prefer from variant data itself
    omim = ""
    inheritance = ""
    for v in variants:
        if not omim:
            omim = str(v.get("omim_number") or v.get("omim") or "").replace("OMIM:", "").strip()
        if not inheritance:
            inheritance = str(v.get("inheritance") or v.get("expected_inheritance") or "").strip()

    if not all_diseases and not dm_hits and not plp_hits:
        return None, f"No HGMD/ClinVar hits for {gene_sym} variants"

    row = {
        "gene_symbol":        gene_sym,
        "disorder":           disorder,
        "omim_number":        omim,
        "inheritance":        inheritance,
        "disease_association": disease_association,
        "function_summary":   function_summary,
    }
    return row, None


def refresh_gene_knowledge(
    gene: str,
    db_path: str,
    *,
    provider: str = "gemini",
    api_key: str = "",
    model: str = "",
    ollama_base_url: str = "http://host.docker.internal:11434/v1",
    # local provider params
    variants: Optional[List[Dict]] = None,
    hgmd_vcf: str = "",
    clinvar_vcf: str = "",
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Unified gene knowledge refresh — routes to Gemini, Ollama, or local VCF files.

    Args:
        provider: "gemini" | "ollama" | "local"
        api_key:  Gemini API key (ignored for ollama/local)
        model:    model name (e.g. "gemini-2.5-flash" or "qwen2.5:32b")
        ollama_base_url: base URL for Ollama endpoint
        variants:    list of variant dicts (required for provider="local")
        hgmd_vcf:    path to HGMD VCF (for provider="local")
        clinvar_vcf: path to ClinVar VCF (for provider="local")
    """
    g = (gene or "").strip().upper()
    if not g or not db_path:
        return None, "missing gene or db path"
    init_gene_knowledge_database(db_path)

    if provider == "local":
        flat, err = fetch_gene_knowledge_from_vcf_files(
            g,
            variants=variants or [],
            hgmd_vcf=hgmd_vcf,
            clinvar_vcf=clinvar_vcf,
        )
    elif provider == "ollama":
        effective_model = model or "qwen2.5:32b"
        flat, err = fetch_gene_knowledge_via_openai_compat(
            g, model=effective_model, base_url=ollama_base_url
        )
    else:
        if not (api_key or "").strip():
            return read_gene_knowledge_full_row(g, db_path), "API key not set"
        effective_model = model or "gemini-2.5-flash"
        flat, err = fetch_gene_knowledge_via_gemini(g, api_key, model=effective_model)

    if not flat:
        return read_gene_knowledge_full_row(g, db_path), err

    upsert_gene_data(
        db_path,
        {
            "gene_symbol": flat["gene_symbol"],
            "function_summary": flat.get("function_summary") or "",
            "disease_association": flat.get("disease_association") or "",
            "omim_number": flat.get("omim_number") or "",
            "inheritance": flat.get("inheritance") or "",
            "disorder": flat.get("disorder") or "",
        },
    )
    return read_gene_knowledge_full_row(g, db_path), None


def get_or_fetch_gene_diseases(
    gene: str,
    db_path: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
    *,
    allow_gemini: bool = True,
) -> List[Dict[str, Any]]:
    """
    Read cache; if empty and ``allow_gemini``, call Gemini, store row, re-read.

    Use for annotator fallback when panel has no row for this gene.
    """
    cached = diseases_from_gene_knowledge_sqlite(gene, db_path)
    if cached:
        return cached
    if not allow_gemini or not api_key:
        return []
    flat, _err = fetch_gene_knowledge_via_gemini(gene, api_key, model=model)
    if not flat:
        return []
    upsert_gene_data(
        db_path,
        {
            "gene_symbol": flat["gene_symbol"],
            "function_summary": flat.get("function_summary") or "",
            "disease_association": flat.get("disease_association") or "",
            "omim_number": flat.get("omim_number") or "",
            "inheritance": flat.get("inheritance") or "",
            "disorder": flat.get("disorder") or "",
        },
    )
    return diseases_from_gene_knowledge_sqlite(gene, db_path)


def enrich_confirmed_variants_for_report(
    variants: List[Dict[str, Any]],
    *,
    gene_knowledge_db: str,
    gemini_api_key: str = "",
    model: str = "gemini-2.5-flash",
    allow_gemini: bool = True,
) -> List[Dict[str, Any]]:
    """
    Merge disorder / inheritance from SQLite (+ optional Gemini) into each variant.
    Intended for **reviewer-confirmed** variants only (portal → Generate Report).

    Call order is per unique gene (cached) to avoid duplicate Gemini requests.
    """
    if not gene_knowledge_db:
        return variants
    init_gene_knowledge_database(gene_knowledge_db)
    gene_cache: Dict[str, List[Dict[str, Any]]] = {}
    out: List[Dict[str, Any]] = []
    for v in variants:
        nv = dict(v)
        gene = (nv.get("gene") or "").strip().upper()
        if not gene:
            out.append(nv)
            continue
        dlist = nv.get("diseases") or []
        has_panel = False
        for d in dlist:
            if isinstance(d, dict) and ((d.get("name") or d.get("disease_name") or "").strip()):
                has_panel = True
                break
            if isinstance(d, str) and d.strip():
                has_panel = True
                break
        has_inh = bool((nv.get("inheritance") or "").strip())
        if has_panel and has_inh:
            out.append(nv)
            continue
        if gene not in gene_cache:
            gene_cache[gene] = get_or_fetch_gene_diseases(
                gene,
                gene_knowledge_db,
                gemini_api_key,
                model=model,
                allow_gemini=allow_gemini,
            )
        gk = gene_cache[gene]
        if not gk:
            out.append(nv)
            continue
        if not has_panel:
            nv["diseases"] = gk
        inh = (gk[0].get("inheritance") or "").strip()
        if not has_inh and inh:
            nv["inheritance"] = inh
        out.append(nv)
    return out
