#!/usr/bin/env python3
"""Build proactive_pgx_gene_drugs_mapping.json from CPIC reference + ClinPGx custom panel."""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "data", "db", "proactive_pgx_gene_drugs_mapping.json")
TSV_PATH = os.path.join(ROOT, "data", "db", "pgx_custom_variants.tsv")

# PharmCAT genes on proactive health panel (minus PDF gene-list exclusions).
PHARMCAT_GENES = [
    "ABCG2", "CACNA1S", "CYP2B6", "CYP2C19", "CYP2C9", "CYP2D6", "CYP3A4", "CYP3A5",
    "CYP4F2", "DPYD", "G6PD", "NAT2", "NUDT15", "RYR1", "SLCO1B1", "TPMT", "UGT1A1", "VKORC1",
]
EXCLUDE = {"MT-RNR1", "CFTR", "HLA-A", "HLA-B", "CES1", "IFNL3"}

# Representative CPIC/DPWG drugs (PharmCAT core genes).
CPIC_GENE_DRUGS: dict[str, str] = {
    "CYP2C9": "warfarin, celecoxib, phenytoin, NSAIDs",
    "CYP2D6": "codeine, tramadol, tamoxifen, TCAs, ondansetron",
    "CYP3A5": "tacrolimus",
    "SLCO1B1": "simvastatin, atorvastatin, rosuvastatin",
    "UGT1A1": "irinotecan, atazanavir",
    "CYP2C19": "clopidogrel, voriconazole, SSRIs, PPIs",
    "DPYD": "fluoropyrimidines (5-FU, capecitabine)",
    "TPMT": "azathioprine, mercaptopurine, thioguanine",
    "NUDT15": "azathioprine, mercaptopurine, thioguanine",
    "CYP2B6": "efavirenz",
    "VKORC1": "warfarin",
    "ABCG2": "rosuvastatin, topotecan, sulfasalazine",
    "CACNA1S": "volatile anesthetics, succinylcholine",
    "CYP3A4": "tacrolimus, statins, cyclosporine, midazolam",
    "CYP4F2": "warfarin",
    "G6PD": "rasburicase, dapsone, primaquine, sulfonamides",
    "NAT2": "isoniazid, hydralazine, sulfasalazine",
    "RYR1": "volatile anesthetics, succinylcholine, caffeine",
}


def _merge_drug_strings(*parts: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if not part or not str(part).strip():
            continue
        for token in re.split(r"[,;/]+", str(part)):
            t = token.strip()
            if not t:
                continue
            key = t.lower()
            if key not in seen:
                seen.add(key)
                out.append(t)
    return ", ".join(out)


def _load_custom_tsv() -> dict[str, str]:
    by_gene: dict[str, list[str]] = {}
    if not os.path.isfile(TSV_PATH):
        return {}
    with open(TSV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 9:
                continue
            gene = cols[0].strip().upper()
            drugs = cols[8].strip()
            if gene and drugs and drugs.lower() not in ("limited clinical data",):
                by_gene.setdefault(gene, []).append(drugs)
    return {g: _merge_drug_strings(*parts) for g, parts in by_gene.items()}


def build() -> dict:
    custom = _load_custom_tsv()
    genes: list[dict] = []
    for gene in sorted(set(PHARMCAT_GENES) | set(custom) - EXCLUDE):
        drugs = _merge_drug_strings(CPIC_GENE_DRUGS.get(gene, ""), custom.get(gene, ""))
        if not drugs:
            drugs = "See CPIC / DPWG / ClinPGx guidelines"
        genes.append({"gene": gene, "drugs_en": drugs, "drugs_cn": drugs, "drugs_ko": drugs})
    return {"genes": genes, "count": len(genes)}


def main() -> None:
    data = build()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {OUT_PATH} ({data['count']} genes)")


if __name__ == "__main__":
    main()
