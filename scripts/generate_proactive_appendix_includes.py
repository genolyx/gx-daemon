#!/usr/bin/env python3
"""Regenerate baked-in proactive appendix HTML from proactive_gene_disease_mapping.json."""

from __future__ import annotations

import html
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "data", "db", "proactive_gene_disease_mapping.json")
PGX_JSON_PATH = os.path.join(ROOT, "data", "db", "proactive_pgx_gene_drugs_mapping.json")
OUT_DIR = os.path.join(ROOT, "data", "report_templates", "includes")

CATEGORY_ICON = {"cancer": "◆", "cardio": "♥", "other": "●", "pgx": "◈"}
COUNT_LABEL = {
    "EN": {"cancer": "cancer genes", "cardio": "cardiovascular genes", "other": "metabolic & other genes"},
    "CN": {"cancer": "个肿瘤易感基因", "cardio": "个心血管基因", "other": "个代谢及其他基因"},
    "KO": {"cancer": "개 암 관련", "cardio": "개 심혈관", "other": "개 대사·기타"},
}
INTRO_TITLE = {
    "EN": "Appendix — Genes Tested",
    "CN": "附錄 — 檢測基因列表",
    "KO": "부록 — 분석 대상 유전자",
}


def _icon_span(cat_id: str) -> str:
    if cat_id == "pgx":
        return '<span class="appendix-cat-icon appendix-cat-icon-pgx"></span>'
    icon = CATEGORY_ICON.get(cat_id, "●")
    return f'<span class="appendix-cat-icon appendix-cat-icon-{cat_id}">{icon}</span>'


def _summary_line(lang: str, pgx_count: int) -> str:
    pgx_icon = _icon_span("pgx")
    if lang == "CN":
        return (
            f'共 <strong>163</strong> 个遗传病相关基因 · {_icon_span("cancer")} 63 肿瘤易感 · '
            f'{_icon_span("cardio")} 66 心血管 · {_icon_span("other")} 34 代谢及其他 · '
            f'{pgx_icon} {pgx_count} 药物基因体学'
        )
    if lang == "KO":
        return (
            f'유전性 질환 <strong>163</strong>개 · {_icon_span("cancer")} 63 암 · '
            f'{_icon_span("cardio")} 66 심혈관 · {_icon_span("other")} 34 대사·기타 · '
            f'{pgx_icon} {pgx_count} PGx'
        )
    return (
        f'<strong>163</strong> hereditary genes · {_icon_span("cancer")} 63 cancer · '
        f'{_icon_span("cardio")} 66 cardiovascular · {_icon_span("other")} 34 metabolic &amp; other · '
        f'{pgx_icon} {pgx_count} pharmacogenomics'
    )


def _label(cat: dict, lang: str) -> str:
    if lang == "CN":
        return (cat.get("label_cn") or cat.get("label_en") or cat.get("id") or "").strip()
    if lang == "KO":
        return (cat.get("label_ko") or cat.get("label_en") or cat.get("id") or "").strip()
    return (cat.get("label_en") or cat.get("id") or "").strip()


def _pair_rows(rows: list) -> list:
    """Two gene|disease pairs per table row."""
    split_at = (len(rows) + 1) // 2
    left, right = rows[:split_at], rows[split_at:]
    return [
        (left[i] if i < len(left) else None, right[i] if i < len(right) else None)
        for i in range(split_at)
    ]


def _build_groups(raw: dict, lang: str) -> list[dict]:
    categories = sorted(raw.get("categories") or [], key=lambda c: int(c.get("order") or 99))
    gene_cat: dict[str, str] = {}
    gene_disorder: dict[str, dict[str, str]] = {}
    for d in raw.get("diseases") or []:
        cat = (d.get("category") or "other").strip().lower()
        lang_fields = {
            "EN": (d.get("disease_name") or "").strip(),
            "CN": (d.get("disease_name_cn") or d.get("disease_name") or "").strip(),
            "KO": (d.get("disease_name_ko") or d.get("disease_name") or "").strip(),
        }
        for g in d.get("genes") or []:
            gk = str(g).strip().upper()
            if gk:
                gene_cat[gk] = cat
                gene_disorder[gk] = lang_fields

    by_cat: dict[str, list] = {c["id"]: [] for c in categories}
    for gene in sorted(gene_disorder):
        cat = gene_cat.get(gene, "other")
        by_cat.setdefault(cat, []).append((gene, gene_disorder[gene][lang] or "—"))

    groups: list[dict] = []
    for cat in categories:
        cid = cat["id"]
        rows = by_cat.get(cid) or []
        if rows:
            groups.append(
                {
                    "id": cid,
                    "title": _label(cat, lang),
                    "count": len(rows),
                    "pairs": _pair_rows(rows),
                }
            )
    return groups


def _render_pair_row(left, right, row_idx: int, *, value_key: str = "disorder") -> list[str]:
    row_class = "appendix-row-even" if row_idx % 2 == 0 else "appendix-row-odd"
    parts = [f'      <tr class="{row_class}">']
    for cell, split in ((left, False), (right, True)):
        split_cls = " split-col" if split else ""
        if cell:
            gene, label = cell
            parts.append(f'        <td class="gene-col{split_cls}">{html.escape(gene)}</td>')
            parts.append(f'        <td class="cond-col">{html.escape(label)}</td>')
        else:
            parts.append(f'        <td class="gene-col{split_cls}"></td>')
            parts.append(f'        <td class="cond-col"></td>')
    parts.append("      </tr>")
    return parts


def _render_pgx_section(lang: str, pgx_raw: dict) -> list[str]:
    drug_key = {"EN": "drugs_en", "CN": "drugs_cn", "KO": "drugs_ko"}[lang]
    th_gene = {"EN": "Gene", "CN": "基因", "KO": "유전자"}[lang]
    th_drugs = {
        "EN": "Representative drug(s)",
        "CN": "代表性相关药物",
        "KO": "대표 관련 약물",
    }[lang]
    rows = [
        (g["gene"], g.get(drug_key) or g.get("drugs_en") or "—")
        for g in pgx_raw.get("genes") or []
        if g.get("gene")
    ]
    count = len(rows)
    if lang == "EN":
        header_text = f"Pharmacogenomics ({count} pharmacogenes)"
    elif lang == "KO":
        header_text = f"약물유전체 ({count}개 PGx 유전자)"
    else:
        header_text = f"药物基因体学（{count}个PGx基因）"
    pairs = _pair_rows(rows)
    parts = [
        '<div class="new-page appendix-section-page appendix-section-pgx">',
        f'  <div class="appendix-section-header appendix-cat-pgx">'
        f'{_icon_span("pgx")} {html.escape(header_text)}</div>',
        '  <table class="summary-table appendix-dual-table">',
        "    <thead><tr>",
        f'      <th class="gene-col">{th_gene}</th><th class="cond-col">{th_drugs}</th>',
        f'      <th class="gene-col split-col">{th_gene}</th><th class="cond-col">{th_drugs}</th>',
        "    </tr></thead>",
        "    <tbody>",
    ]
    for row_idx, (left, right) in enumerate(pairs):
        parts.extend(_render_pair_row(left, right, row_idx))
    parts.extend(["    </tbody>", "  </table>", "</div>"])
    return parts


def _render_section_table(lang: str, group: dict, *, with_intro: bool = False, pgx_count: int = 0) -> list[str]:
    th_gene = {"EN": "Gene", "CN": "基因", "KO": "유전자"}[lang]
    th_dis = {
        "EN": "Associated condition",
        "CN": "相關疾病",
        "KO": "관련 질환",
    }[lang]
    cid = group["id"]
    count_lbl = COUNT_LABEL[lang].get(cid, "")
    if lang == "EN":
        header_text = f'{group["title"]} ({group["count"]} {count_lbl})'
    elif lang == "KO":
        header_text = f'{group["title"]} ({group["count"]} {count_lbl})'
    else:
        header_text = f'{group["title"]}（{group["count"]}{count_lbl}）'

    parts = [f'<div class="new-page appendix-section-page appendix-section-{cid}">']
    if with_intro:
        parts.append(f'  <div class="section-title">{html.escape(INTRO_TITLE[lang])}</div>')
        parts.append(f'  <p class="appendix-summary-line">{_summary_line(lang, pgx_count)}</p>')
    parts.extend([
        f'  <div class="appendix-section-header appendix-cat-{cid}">'
        f'{_icon_span(cid)} {html.escape(header_text)}</div>',
        '  <table class="summary-table appendix-dual-table">',
        "    <thead><tr>",
        f'      <th class="gene-col">{th_gene}</th><th class="cond-col">{th_dis}</th>',
        f'      <th class="gene-col split-col">{th_gene}</th><th class="cond-col">{th_dis}</th>',
        "    </tr></thead>",
        "    <tbody>",
    ])
    for row_idx, (left, right) in enumerate(group["pairs"]):
        parts.extend(_render_pair_row(left, right, row_idx))
    parts.extend(["    </tbody>", "  </table>", "</div>"])
    return parts


def _render_lang(groups: list[dict], lang: str, pgx_raw: dict) -> str:
    pgx_count = int(pgx_raw.get("count") or len(pgx_raw.get("genes") or []))
    parts = ["<!-- Auto-generated from proactive_gene_disease_mapping.json — do not edit by hand -->"]
    for idx, group in enumerate(groups):
        parts.extend(_render_section_table(lang, group, with_intro=(idx == 0), pgx_count=pgx_count))
    parts.extend(_render_pgx_section(lang, pgx_raw))
    return "\n".join(parts) + "\n"


def main() -> None:
    with open(JSON_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    if not os.path.isfile(PGX_JSON_PATH):
        raise SystemExit(f"Missing {PGX_JSON_PATH} — run scripts/build_proactive_pgx_gene_drugs_mapping.py first")
    with open(PGX_JSON_PATH, encoding="utf-8") as f:
        pgx_raw = json.load(f)
    os.makedirs(OUT_DIR, exist_ok=True)
    for lang, fname in [
        ("EN", "proactive_appendix_genes_EN.html"),
        ("CN", "proactive_appendix_genes_CN.html"),
        ("KO", "proactive_appendix_genes_KO.html"),
    ]:
        path = os.path.join(OUT_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_render_lang(_build_groups(raw, lang), lang, pgx_raw))
        print("Wrote", path)


if __name__ == "__main__":
    main()
