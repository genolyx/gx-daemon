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
    Build gene knowledge from HGMD + ClinVar VCF files (no AI required).

    HGMD: full gene-level scan — collects ALL DM/DM? entries for the gene
          regardless of whether they appear in result.json.
    ClinVar: position-based lookup using variants from result.json.

    Args:
        gene:        gene symbol (e.g. "FGFR3")
        variants:    list of {"chrom", "pos", "ref", "alt", ...} dicts from result.json
        hgmd_vcf:    path to tabix-indexed HGMD VCF (.vcf.gz)
        clinvar_vcf: path to tabix-indexed ClinVar VCF (.vcf.gz)
    """
    from .annotator import ClinVarAnnotator, HGMDAnnotator

    gene_sym = (gene or "").strip().upper()
    if not gene_sym:
        return None, "missing gene"

    hgmd_ann    = HGMDAnnotator(hgmd_vcf)      if hgmd_vcf    and os.path.isfile(hgmd_vcf)    else None
    clinvar_ann = ClinVarAnnotator(clinvar_vcf) if clinvar_vcf and os.path.isfile(clinvar_vcf) else None

    if not hgmd_ann and not clinvar_ann:
        return None, "no annotation files available (set HGMD_VCF and/or CLINVAR_VCF)"

    def _uniq(seq):
        seen: set = set()
        return [x for x in seq if x and x not in seen and not seen.add(x)]

    # ── HGMD: full gene scan (all known DM/DM? entries) ─────────
    hgmd_hits: List[Dict] = []
    if hgmd_ann and os.path.isfile(hgmd_vcf):
        try:
            import pysam as _pysam
            vf = _pysam.VariantFile(hgmd_vcf)
            for rec in vf.fetch():
                info = rec.info
                g_raw = info.get("GENE", "")
                g = g_raw if isinstance(g_raw, str) else (g_raw[0] if g_raw else "")
                if g.upper() != gene_sym:
                    continue
                cls_raw  = info.get("CLASS",   "")
                dis_raw  = info.get("DISEASE", "")
                pmid_raw = info.get("PMID",    "")
                hgvsc_raw= info.get("HGVSC",   "")
                hgmdid   = info.get("HGMDID",  "")
                cls  = cls_raw  if isinstance(cls_raw,  str) else (cls_raw[0]  if cls_raw  else "")
                dis  = dis_raw  if isinstance(dis_raw,  str) else (dis_raw[0]  if dis_raw  else "")
                hgvsc= hgvsc_raw if isinstance(hgvsc_raw,str) else (hgvsc_raw[0] if hgvsc_raw else "")
                hgmdid_s = hgmdid if isinstance(hgmdid, str) else (hgmdid[0] if hgmdid else "")
                # pysam returns INFO tuples — flatten all elements and filter '.' nulls
                if isinstance(pmid_raw, str):
                    pmid_parts = pmid_raw.split(",")
                else:
                    pmid_parts = []
                    for item in (pmid_raw or []):
                        pmid_parts.extend(str(item).split(","))
                pmids = [p.strip() for p in pmid_parts if p.strip() and p.strip() != "."]
                hgmd_hits.append({
                    "hgmd_class":   cls,
                    "hgmd_disease": dis,
                    "hgmd_pmid":    ",".join(pmids[:5]),
                    "hgmd_hgvsc":   hgvsc,
                    "hgmd_id":      hgmdid_s,
                })
            vf.close()
        except Exception as e:
            logger.debug("HGMD gene scan error for %s: %s", gene_sym, e)

    dm_hits = [h for h in hgmd_hits if h.get("hgmd_class") in ("DM", "DM?")]
    # Sort: confirmed DM first, then DM?
    dm_hits.sort(key=lambda h: (0 if h.get("hgmd_class") == "DM" else 1))

    # ── ClinVar: position lookup for variants in result.json ─────
    clinvar_hits: List[Dict] = []
    if clinvar_ann and variants:
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
            cr = clinvar_ann.lookup(chrom, pos, ref, alt)
            if cr:
                clinvar_hits.append(cr)

    # ── Aggregate diseases ───────────────────────────────────────
    _SKIP = {"not_provided", "not_specified", "not provided", "not specified"}

    def _clean_disease(raw: str) -> List[str]:
        """Split ClinVar '|'-separated CLNDN, strip underscores, drop junk."""
        parts = []
        for p in raw.replace("_", " ").split("|"):
            p = p.strip()
            if p and p.lower() not in _SKIP:
                parts.append(p)
        return parts

    # Separate confirmed DM from probable DM?
    dm_confirmed = [h for h in dm_hits if h.get("hgmd_class") == "DM"]
    dm_probable  = [h for h in dm_hits if h.get("hgmd_class") == "DM?"]

    diseases_dm = _uniq([
        h.get("hgmd_disease", "").replace("_", " ").strip()
        for h in dm_confirmed
        if h.get("hgmd_disease", "").strip().lower() not in _SKIP
    ])
    diseases_dm_q = _uniq([
        h.get("hgmd_disease", "").replace("_", " ").strip()
        for h in dm_probable
        if h.get("hgmd_disease", "").strip().lower() not in _SKIP
    ])
    diseases_hgmd = _uniq(diseases_dm + [d for d in diseases_dm_q if d not in diseases_dm])

    plp_hits = [
        c for c in clinvar_hits
        if "pathogenic" in (c.get("clnsig_primary") or "").lower()
    ]
    conditions_cv: List[str] = []
    for c in plp_hits:
        for d in _clean_disease(c.get("clndn") or ""):
            if d not in conditions_cv:
                conditions_cv.append(d)

    cv_stars = max((int(c.get("stars") or 0) for c in plp_hits), default=0)

    all_diseases = _uniq(diseases_hgmd + [d for d in conditions_cv if d not in diseases_hgmd])
    disorder = ", ".join(all_diseases[:5]) if all_diseases else ""

    # ── Evidence summary ─────────────────────────────────────────
    evidence_parts: List[str] = []
    if dm_hits:
        evidence_parts.append(
            f"HGMD: {len(dm_hits)} disease-causing variant(s) "
            f"(DM: {len(dm_confirmed)}, DM?: {len(dm_probable)})"
        )
    if plp_hits:
        stars_str = " " + "★" * cv_stars if cv_stars else ""
        evidence_parts.append(
            f"ClinVar: {len(plp_hits)} P/LP variant(s){stars_str}"
        )
    disease_association = "; ".join(evidence_parts) if evidence_parts else (
        "No pathogenic entries found in HGMD or ClinVar for this gene."
    )

    # PMIDs: confirmed DM first, then DM?, deduplicated, max 5
    all_pmids: List[str] = []
    for h in dm_confirmed + dm_probable:
        for p in (h.get("hgmd_pmid") or "").split(","):
            p = p.strip()
            if p and p not in all_pmids:
                all_pmids.append(p)
    all_pmids = all_pmids[:5]

    fs_parts: List[str] = []
    if diseases_dm:
        fs_parts.append(f"HGMD (DM — confirmed): {', '.join(diseases_dm[:5])}.")
    if diseases_dm_q:
        fs_parts.append(f"HGMD (DM? — probable): {', '.join(diseases_dm_q[:5])}.")
    if conditions_cv:
        fs_parts.append(f"ClinVar P/LP: {', '.join(conditions_cv[:5])}.")
    if all_pmids:
        fs_parts.append(f"Key HGMD references (PMID): {', '.join(all_pmids)}.")
    function_summary = " ".join(fs_parts) if fs_parts else ""

    # inheritance / OMIM from variant data
    omim = ""
    inheritance = ""
    for v in (variants or []):
        if not omim:
            omim = str(v.get("omim_number") or v.get("omim") or "").replace("OMIM:", "").strip()
        if not inheritance:
            inheritance = str(v.get("inheritance") or v.get("expected_inheritance") or "").strip()

    if not all_diseases and not dm_hits and not plp_hits:
        return None, f"No HGMD/ClinVar hits found for gene {gene_sym}"

    row = {
        "gene_symbol":         gene_sym,
        "disorder":            disorder,
        "omim_number":         omim,
        "inheritance":         inheritance,
        "disease_association": disease_association,
        "function_summary":    function_summary,
    }
    return row, None


def _get_hgmd_pmids_for_gene(gene: str, hgmd_vcf: str, max_pmids: int = 5) -> List[str]:
    """
    Scan HGMD VCF for all DM/DM? entries for the given gene.
    Returns up to max_pmids unique PMIDs, confirmed DM PMIDs first.
    """
    if not hgmd_vcf or not os.path.isfile(hgmd_vcf):
        return []
    gene_sym = gene.strip().upper()
    dm_pmids: List[str] = []
    dmq_pmids: List[str] = []
    try:
        import pysam as _pysam
        vf = _pysam.VariantFile(hgmd_vcf)
        for rec in vf.fetch():
            info = rec.info
            g_raw = info.get("GENE", "")
            g = g_raw if isinstance(g_raw, str) else (g_raw[0] if g_raw else "")
            if g.upper() != gene_sym:
                continue
            cls_raw  = info.get("CLASS", "")
            pmid_raw = info.get("PMID",  "")
            cls = cls_raw if isinstance(cls_raw, str) else (cls_raw[0] if cls_raw else "")
            # pysam returns INFO tuples — flatten all elements and split on comma
            if isinstance(pmid_raw, str):
                pmid_parts = pmid_raw.split(",")
            else:
                pmid_parts = []
                for item in (pmid_raw or []):
                    pmid_parts.extend(str(item).split(","))
            for p in pmid_parts:
                p = p.strip()
                if not p or p == ".":
                    continue
                if cls == "DM":
                    if p not in dm_pmids:
                        dm_pmids.append(p)
                elif cls == "DM?":
                    if p not in dmq_pmids:
                        dmq_pmids.append(p)
        vf.close()
    except Exception as e:
        logger.debug("HGMD PMID scan error for %s: %s", gene_sym, e)
    combined: List[str] = []
    for p in dm_pmids + dmq_pmids:
        if p not in combined:
            combined.append(p)
    return combined[:max_pmids]


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
    For Gemini/Ollama providers, HGMD PMIDs are automatically appended to the
    AI-generated function_summary when hgmd_vcf is provided.

    Args:
        provider: "gemini" | "ollama" | "local"
        api_key:  Gemini API key (ignored for ollama/local)
        model:    model name (e.g. "gemini-2.5-flash" or "qwen2.5:32b")
        ollama_base_url: base URL for Ollama endpoint
        variants:    list of variant dicts (required for provider="local")
        hgmd_vcf:    path to HGMD VCF — used for PMID enrichment (all providers)
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

    # ── Append HGMD PMIDs for Gemini/Ollama results ──────────────
    if provider in ("gemini", "ollama") and hgmd_vcf:
        pmids = _get_hgmd_pmids_for_gene(g, hgmd_vcf)
        if pmids:
            pmid_line = f"\n\nReferences (PMID): {', '.join(pmids)}."
            # Append to disease_association (rendered last in UI) so PMIDs appear at the bottom
            existing_da = (flat.get("disease_association") or "").rstrip()
            flat["disease_association"] = existing_da + pmid_line

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


def _fetch_inheritance_from_gemini(gene: str, gemini_api_key: str, model: str) -> str:
    """
    Targeted Gemini query to get inheritance pattern when DB entry is missing it.
    Returns a short code like 'AD', 'AR', 'XL', or empty string on failure.
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=gemini_api_key)
        prompt = (
            f"What is the inheritance pattern of the human disease gene {gene}? "
            f"Reply with only a JSON object, no markdown: "
            f'{{\"inheritance\": \"<one of: AD, AR, XL, XLD, XLR, MT, or unknown>\"}}'
        )
        gm = genai.GenerativeModel(model)
        resp = gm.generate_content(prompt)
        text = (resp.text or "").strip()
        # strip markdown code fences if present
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        import json as _json
        data = _json.loads(text)
        inh = str(data.get("inheritance") or "").strip()
        if inh and inh.lower() != "unknown":
            return inh
    except Exception as e:
        logger.debug("Gemini inheritance query failed for %s: %s", gene, e)
    return ""


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
        if not has_inh and not inh and allow_gemini and gemini_api_key:
            # DB has no inheritance — targeted Gemini query for just the inheritance field
            inh = _fetch_inheritance_from_gemini(gene, gemini_api_key, model)
            if inh:
                logger.info("[gene_knowledge] Filled missing inheritance for %s: %s", gene, inh)
                # Persist back to DB so future reports don't need to re-query
                try:
                    with sqlite3.connect(gene_knowledge_db) as _conn:
                        _conn.execute(
                            "UPDATE gene_data SET inheritance = ? WHERE gene_symbol = ? AND (inheritance IS NULL OR inheritance = '')",
                            (inh, gene),
                        )
                except Exception as _e:
                    logger.debug("Failed to persist inheritance for %s: %s", gene, _e)
        if not has_inh and inh:
            nv["inheritance"] = inh
        out.append(nv)
    return out
