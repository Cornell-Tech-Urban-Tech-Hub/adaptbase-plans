#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pymupdf",
#   "anthropic",
#   "openai",
#   "python-dotenv",
#   "tqdm",
# ]
# ///
"""
Triage climate adaptation plans in plans/ by three criteria:
  (a) Geographic scope: city/county/municipal level (NOT national/state/provincial)
  (b) Topic focus: primarily climate adaptation or resilience
      (sustainability/mitigation OK only if a substantial section covers resilience)
  (c) Authority: official government plan (NOT academic paper, white paper, or corporate document)

Pipeline per PDF:
  Stage 1: Filename keyword matching  — fast, handles obvious cases
  Stage 2: PDF title + first 3 pages  — resolves most remaining cases
  Stage 3: Full text scan (20 pages)  — keyword density + chapter headings
  Stage 4: LLM (claude-haiku)         — for genuinely ambiguous cases

Outcomes:
  PASS             → leave in place, update sidecar
  FAIL_GEO         → move to plans/_rejected/, update sidecar with reason
  FAIL_TOPIC       → move to plans/_rejected/, update sidecar with reason
  FAIL_AUTHORITY   → move to plans/_rejected/, update sidecar with reason
  HUMAN_REVIEW     → move to plans/_human_review_needed/, update sidecar

Usage:
  uv run scripts/triage_plans.py [--dry-run] [--force] [--llm-limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import fitz  # pymupdf
from tqdm import tqdm

PLANS_DIR = Path(__file__).parent.parent / "plans"
REJECTED_DIR = PLANS_DIR / "_rejected"
HUMAN_REVIEW_DIR = PLANS_DIR / "_human_review_needed"

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

# Phrases that indicate non-official documents (academic, corporate, consultant)
_FAIL_AUTHORITY_PHRASES = [
    "working paper",
    "discussion paper",
    "white paper",
    "research paper",
    "policy brief",
    "conference paper",
    "peer review",
    "literature review",
    "journal article",
    "doctoral dissertation",
    "master's thesis",
    "phd thesis",
    "commissioned by",
    "prepared by",
    "prepared for",
    "consultant report",
    "consulting report",
    "case study analysis",
    "academic press",
    "university press",
    "all rights reserved",  # often in academic/corporate docs
]

# Publisher/source patterns that indicate academic/non-governmental origin
_FAIL_AUTHORITY_PATTERNS = [
    re.compile(r"\buniversity\s+of\b", re.I),
    re.compile(r"\bacademic\s+journal\b", re.I),
    re.compile(
        r"\b(springer|elsevier|wiley|taylor\s*&\s*francis|sage|oxford\s+university\s+press)\b",
        re.I,
    ),
    re.compile(r"\bissn\s*:?\s*\d{4}[-\s]?\d{3}[\dxX]\b", re.I),  # journal ISSN
    re.compile(r"\bdoi\s*:?\s*10\.\d+/", re.I),  # academic DOI
    re.compile(r"\b(abstract|keywords)\s*:\s*\w+", re.I),  # academic paper format
    re.compile(r"\bsubmitted\s+(to|for)\s+publication\b", re.I),
]

# Phrases that indicate official government documents (PASS authority)
_PASS_AUTHORITY_PHRASES = [
    "city council",
    "city government",
    "municipal government",
    "municipality of",
    "mayor's office",
    "department of",
    "office of sustainability",
    "office of resilience",
    "city planning",
    "urban planning department",
    "adopted by",
    "ordinance",
    "resolution",
    "official plan",
    "statutory plan",
    "master plan",
    "comprehensive plan",
    "general plan",
    "official adoption",
    "council resolution",
]

# Multilingual official government phrases
_PASS_AUTHORITY_PHRASES_ML = [
    # Spanish
    "ayuntamiento",
    "municipalidad",
    "gobierno municipal",
    "concejo municipal",
    "alcaldía",
    "plan oficial",
    "resolución",
    # Portuguese
    "prefeitura",
    "governo municipal",
    "câmara municipal",
    "plano oficial",
    # French
    "mairie",
    "hôtel de ville",
    "conseil municipal",
    "gouvernement municipal",
    "plan officiel",
    "arrêté",
    # German
    "stadtverwaltung",
    "stadtrat",
    "gemeindeverwaltung",
    "bürgermeisteramt",
    "offizieller plan",
    # Italian
    "comune",
    "consiglio comunale",
    "piano ufficiale",
    # Turkish
    "belediye",
    "şehir yönetimi",
    "resmi plan",
]

# Phrases that unambiguously indicate national/state/provincial scope
_FAIL_GEO_PHRASES = [
    "national adaptation plan",
    "national climate adaptation",
    "national adaptation strategy",
    "national resilience strategy",
    "national climate change adaptation",
    "national climate plan",
    "national climate framework",
    "national climate strategy",
    "national climate communication",
    "national communication",
    "national action plan",
    "state climate action",
    "state adaptation plan",
    "state climate adaptation",
    "provincial adaptation",
    "provincial climate",
    "province climate",
    "province adaptation",
    "federal adaptation",
    "federal climate",
    "country climate plan",
    "national determined contribution",
    "nationally determined contribution",
    "biennial update report",
    "biennial transparency report",
]

# Single keywords indicating national scope (only flagged if NOT followed by
# "city" / "capital" / "territory" which might be city-level entities)
_FAIL_GEO_WORDS = {"national", "provincial", "province"}

# Abbreviations that, as standalone tokens, indicate national-level documents
_FAIL_GEO_ABBREVS = {"ndc", "btr", "bur"}

# Phrases that clearly indicate city/municipal scope (help PASS criterion a)
_PASS_GEO_PHRASES = [
    "city climate",
    "city adaptation",
    "city resilience",
    "city plan",
    "city strategy",
    "municipal climate",
    "municipal adaptation",
    "municipal resilience",
    "county climate",
    "county adaptation",
    "urban resilience",
    "urban adaptation",
    "urban climate",
    "community resilience",
    "community adaptation",
    "local adaptation",
    "local climate",
]

# Phrases that clearly indicate adaptation/resilience focus (topic PASS)
_PASS_TOPIC_PHRASES = [
    "resilience strategy",
    "climate adaptation",
    "adaptation plan",
    "adaptation strategy",
    "climate resilience",
    "climate resilient",
    "resilient city",
    "resilient cities",
    "resilient community",
    "resilient urban",
    "resilience plan",
    "climate vulnerability",
    "vulnerability assessment",
    "hazard mitigation",  # emergency-mgmt term, NOT GHG mitigation
    "disaster risk reduction",
    "disaster resilience",
    "flood resilience",
    "heat resilience",
    "coastal resilience",
    "climate risk assessment",
    "climate preparedness",
    "climate risk",
    "resilience framework",
    "urban heat",
    "urban cooling",
    "cooling strategy",
]

# Phrases indicating a mitigation/sustainability focus at stage 2
# (used as FAIL only when PASS_TOPIC signals are absent)
# Note: "climate action plan" is NOT here because many CAPs include adaptation
# chapters — vocabulary analysis in stage 3 handles those.
_FAIL_TOPIC_PHRASES = [
    "net zero",
    "net-zero",
    "carbon neutral",
    "decarbonization",
    "decarbonisation",
    "zero carbon",
    "ghg reduction",
    "greenhouse gas reduction",
    "emissions reduction plan",
    "low carbon roadmap",
    "carbon roadmap",
    "sustainability plan",
    "sustainable development goals",
    "sdg report",
    "carbon action plan",
    "low-carbon development",
]

# ---------------------------------------------------------------------------
# Multilingual keywords (Spanish, Portuguese, French, German, Dutch, Italian,
# Turkish) — these extend stages 1-3 for non-English documents.
# ---------------------------------------------------------------------------

# GEO FAIL in non-English
_FAIL_GEO_PHRASES_ML = [
    # Spanish/Portuguese
    "plan nacional",
    "adaptación nacional",
    "estrategia nacional",
    "política nacional",
    "programa nacional",
    "plan de adaptación nacional",
    "adaptação nacional",
    "estratégia nacional",
    "programa nacional de adaptação",
    # French
    "plan national",
    "stratégie nationale",
    "programme national",
    "adaptation nationale",
    "communication nationale",
    # German/Dutch
    "nationaler aktionsplan",
    "nationale strategie",
    "nationale anpassungsstrategie",
    "bundesweite",
    "landesstrategie",
    "nationaal klimaatplan",
    # Italian
    "piano nazionale",
    "strategia nazionale",
    # Turkish
    "ulusal uyum",
    "ulusal iklim",
]

# GEO FAIL single words in non-English
_FAIL_GEO_WORDS_ML = {
    "nacional",  # Spanish/Portuguese/Italian
    "nationale",  # French/German/Dutch
    "provincial",  # same across many languages
    "provincial",
    "estadual",  # Portuguese (state-level)
    "bundesland",  # German state
    "eyalet",  # Turkish state
}

# TOPIC PASS in non-English
_PASS_TOPIC_PHRASES_ML = [
    # Spanish
    "adaptación climática",
    "resiliencia climática",
    "adaptación al cambio climático",
    "plan de resiliencia",
    "estrategia de resiliencia",
    "plan de adaptación",
    "riesgo climático",
    "vulnerabilidad climática",
    "gestión de riesgo",
    "resiliencia urbana",
    "adaptación municipal",
    # Portuguese
    "adaptação climática",
    "resiliência climática",
    "adaptação às mudanças climáticas",
    "plano de resiliência",
    "plano de adaptação",
    "risco climático",
    "vulnerabilidade climática",
    "resiliência urbana",
    # French
    "adaptation climatique",
    "résilience climatique",
    "adaptation au changement climatique",
    "plan de résilience",
    "risque climatique",
    "vulnérabilité climatique",
    "adaptation locale",
    "résilience urbaine",
    # German/Dutch
    "klimaanpassung",
    "klimaresilienz",
    "anpassung an den klimawandel",
    "klimaanpassungsplan",
    "resilienzstrategie",
    "klimawandel anpassung",
    "klimaatadaptatie",
    "klimaatresiliëntie",
    "klimaatrisico",
    # Italian
    "adattamento climatico",
    "resilienza climatica",
    "piano di adattamento",
    "rischio climatico",
    "vulnerabilità climatica",
    # Turkish
    "iklim uyumu",
    "iklim dayanıklılığı",
    "iklim adaptasyonu",
    "uyum planı",
    "dayanıklı kent",
]

# TOPIC FAIL in non-English (strong mitigation/sustainability signals)
_FAIL_TOPIC_PHRASES_ML = [
    # Spanish
    "cero emisiones",
    "carbono neutro",
    "descarbonización",
    "cero neto",
    "reducción de emisiones",
    "neutralidad climática",
    # Portuguese
    "zero emissões",
    "carbono neutro",
    "descarbonização",
    "net zero",
    "redução de emissões",
    "neutralidade climática",
    # French
    "zéro émission",
    "carbone neutre",
    "décarbonation",
    "net zéro",
    "réduction des émissions",
    "neutralité climatique",
    # German
    "klimaneutral",
    "netto-null",
    "treibhausgasneutral",
    "emissionsreduzierung",
    "zero-carbon",
    "co2-neutral",
    # Dutch
    "klimaatneutraal",
    "netto-nul",
    "emissie reductie",
    # Italian
    "neutralità climatica",
    "zero emissioni",
    "carbonio neutro",
    # Turkish
    "sıfır emisyon",
    "karbon nötr",
    "net sıfır",
]


# Pre-compute diacritic-normalized versions of the multilingual phrase lists
# so matching works against text with or without accents.
def _norm_phrases(phrases: list[str]) -> list[str]:
    import unicodedata as _ud

    return [
        "".join(c for c in _ud.normalize("NFD", p) if _ud.category(c) != "Mn")
        for p in phrases
    ]


_FAIL_GEO_PHRASES_ML_NORM = _norm_phrases(_FAIL_GEO_PHRASES_ML)
_PASS_TOPIC_PHRASES_ML_NORM = _norm_phrases(_PASS_TOPIC_PHRASES_ML)
_FAIL_TOPIC_PHRASES_ML_NORM = _norm_phrases(_FAIL_TOPIC_PHRASES_ML)
_PASS_AUTHORITY_PHRASES_ML_NORM = _norm_phrases(_PASS_AUTHORITY_PHRASES_ML)

# Chapter/section headings that, if found in full text, indicate a substantial
# adaptation/resilience section (used in stage 3 for mixed documents)
_RESILIENCE_CHAPTER_PATTERNS = [
    re.compile(
        r"\b(chapter|section)\s+\d+[:\s]+.{0,60}(resilience|adaptation)\b", re.I
    ),
    re.compile(r"\b(resilience|adaptation)\b.{0,60}(chapter|section)\s+\d+", re.I),
    re.compile(
        r"^[\d.]+\s+.{0,60}(climate\s+)?(resilience|adaptation)\b", re.I | re.MULTILINE
    ),
]

# Dense adaptation vocabulary (used in stage 3 text-ratio analysis)
# Includes key multilingual terms for Spanish/Portuguese/French/German
_ADAPT_VOCAB = re.compile(
    r"\b(adapt|resilien|vulnerab|flood risk|heat island|urban heat|urban cooling|"
    r"urban overheating|sea.?level|storm surge|drought|extreme weather|climate impact|"
    r"climate hazard|climate risk|preparedness|disaster risk|climate change impact|"
    r"cooling strategy|heat refuge|heat stress|heat wave|heatwave|"
    # Spanish/Portuguese
    r"adaptaci|resilienci|vulnerabilid|riesgo clim|riesgo de inundaci|"
    r"adaptaç|resiliênc|vulnerabilid|risco climát|"
    # French
    r"adaptation climat|résilience|vulnérabilit|risque climati|"
    # German/Dutch
    r"klimaanpassung|klimaresilienz|klimavulnerabilit|klimaanpassung|"
    r"klimaatadaptati|klimaatresiliënt)\w*\b",
    re.I,
)
# Only count clearly GHG/mitigation vocabulary; avoid "mitigation" alone since
# "hazard mitigation" and "heat mitigation" are adaptation concepts
_MITIG_VOCAB = re.compile(
    r"\b(emission|carbon footprint|ghg|greenhouse gas|decarboni|net.?zero|"
    r"renewable energy|solar panel|wind turbine|electrif|carbon neutral|low.?carbon|"
    r"energy transition|clean energy|zero emission|"
    # Multilingual mitigation terms
    r"emision|emisión|emissão|emissões|descarboni|"  # Spanish/Portuguese
    r"neutralité carbone|zéro carbone|décarbona|"  # French
    r"klimaneutral|treibhausgas|netto-null|"  # German
    r"klimaatneutraal|netto-nul)\w*\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Verdict(NamedTuple):
    status: str  # PASS | FAIL_GEO | FAIL_TOPIC | FAIL_AUTHORITY | HUMAN_REVIEW
    stage: str  # filename | pdf_pages | full_text | llm
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _remove_diacritics(s: str) -> str:
    """Remove accents/diacritics for fuzzy matching (e.g. 'adaptación' → 'adaptacion')."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _stem(path: Path) -> str:
    """Lowercase, diacritic-stripped filename stem with separators replaced by spaces."""
    name = path.name.lower()
    name = re.sub(r"\.[^.]+$", "", name)  # strip extension
    name = re.sub(r"[-_.\[\]()]+", " ", name)
    return _remove_diacritics(name)


def _text_lower_norm(text: str) -> str:
    """Lowercase text with diacritics removed for multilingual phrase matching."""
    return _remove_diacritics(text.lower())


def _contains_phrase(text: str, phrase: str) -> bool:
    return phrase in text


def _contains_word(text: str, word: str) -> bool:
    return bool(re.search(r"\b" + re.escape(word) + r"\b", text))


def extract_text(pdf_path: Path, max_pages: int) -> str:
    """Extract text from first max_pages pages using pymupdf."""
    try:
        doc = fitz.open(str(pdf_path))
        pages = min(max_pages, len(doc))
        parts = []
        for i in range(pages):
            parts.append(doc[i].get_text())
        doc.close()
        return "\n".join(parts)
    except Exception as exc:
        return f"[extraction error: {exc}]"


def load_sidecar(sidecar_path: Path) -> dict:
    if sidecar_path.exists():
        try:
            return json.loads(sidecar_path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_sidecar(sidecar_path: Path, data: dict) -> None:
    sidecar_path.write_text(json.dumps(data, indent=2, default=str) + "\n")


# ---------------------------------------------------------------------------
# Stage 1: Filename analysis
# ---------------------------------------------------------------------------


def stage1_filename(pdf_path: Path) -> Verdict | None:
    """Return a definitive Verdict or None (proceed to next stage)."""
    stem = _stem(pdf_path)

    # --- Authority: definitive FAIL (academic/corporate indicators in filename) ---
    fail_authority_in_filename = [
        "working paper",
        "working-paper",
        "workingpaper",
        "discussion paper",
        "discussion-paper",
        "discussionpaper",
        "white paper",
        "white-paper",
        "whitepaper",
        "research paper",
        "research-paper",
        "researchpaper",
        "policy brief",
        "policy-brief",
        "policybrief",
        "case study",
        "case-study",
        "casestudy",
    ]
    for phrase in fail_authority_in_filename:
        if _contains_phrase(stem, phrase):
            return Verdict(
                "FAIL_AUTHORITY",
                "filename",
                f"filename contains '{phrase}' (non-official document)",
            )

    # --- Geography: definitive FAIL ---
    for phrase in _FAIL_GEO_PHRASES:
        if _contains_phrase(stem, phrase):
            return Verdict("FAIL_GEO", "filename", f"filename contains '{phrase}'")

    for word in _FAIL_GEO_WORDS:
        if _contains_word(stem, word):
            # Allow "national city", "national capital territory", "provincial city"
            if not re.search(r"\b" + word + r"\s+(city|capital|territory)\b", stem):
                return Verdict("FAIL_GEO", "filename", f"filename contains '{word}'")

    tokens = set(re.split(r"\s+", stem))
    for abbrev in _FAIL_GEO_ABBREVS:
        if abbrev in tokens:
            return Verdict(
                "FAIL_GEO",
                "filename",
                f"filename contains abbreviation '{abbrev.upper()}'",
            )

    # --- Topic: definitive FAIL (only when no PASS signal present) ---
    has_pass_topic = any(_contains_phrase(stem, p) for p in _PASS_TOPIC_PHRASES)

    if not has_pass_topic:
        # Check for hard fail-topic phrases
        hard_fail_phrases = [
            "net zero",
            "net-zero",
            "carbon neutral",
            "decarboniz",
            "zero carbon",
            "ghg reduction",
            "greenhouse gas reduction",
            "sustainability plan",
            "carbon roadmap",
        ]
        for phrase in hard_fail_phrases:
            phrase_norm = re.sub(r"[-_]", " ", phrase)
            if _contains_phrase(stem, phrase_norm):
                return Verdict(
                    "FAIL_TOPIC",
                    "filename",
                    f"filename contains '{phrase}' with no adaptation signals",
                )

    # stem is already diacritic-normalized (via _stem()); use normalized phrases
    # --- Multilingual GEO FAIL in filename ---
    for orig, norm in zip(_FAIL_GEO_PHRASES_ML, _FAIL_GEO_PHRASES_ML_NORM):
        if _contains_phrase(stem, norm):
            return Verdict(
                "FAIL_GEO",
                "filename",
                f"filename contains multilingual geo-fail phrase '{orig}'",
            )
    for word in _FAIL_GEO_WORDS_ML:
        word_norm = _remove_diacritics(word)
        if _contains_word(stem, word_norm):
            if not re.search(
                r"\b"
                + word_norm
                + r"\s+(city|capital|territory|ville|ciudad|cidade)\b",
                stem,
            ):
                return Verdict(
                    "FAIL_GEO",
                    "filename",
                    f"filename contains '{word}' (national/state indicator)",
                )

    # --- Multilingual TOPIC PASS in filename (no GEO FAIL detected above) ---
    for orig, norm in zip(_PASS_TOPIC_PHRASES_ML, _PASS_TOPIC_PHRASES_ML_NORM):
        if _contains_phrase(stem, norm):
            return Verdict(
                "PASS",
                "filename",
                f"filename contains multilingual adaptation phrase '{orig}'",
            )

    # --- Multilingual TOPIC FAIL in filename (no PASS signal present) ---
    has_pass_topic_ml = has_pass_topic or any(
        _contains_phrase(stem, n) for n in _PASS_TOPIC_PHRASES_ML_NORM
    )
    if not has_pass_topic_ml:
        for orig, norm in zip(_FAIL_TOPIC_PHRASES_ML, _FAIL_TOPIC_PHRASES_ML_NORM):
            if _contains_phrase(stem, norm):
                return Verdict(
                    "FAIL_TOPIC",
                    "filename",
                    f"filename contains '{orig}' with no adaptation signals",
                )

    # --- Strong PASS: both geo and topic clear from filename ---
    # Classic 100RC "City-Resilience-Strategy-English" pattern
    if re.search(r"\bresilience\s+strategy\b", stem):
        # Check no national/state override already done above
        return Verdict(
            "PASS",
            "filename",
            "filename indicates city resilience strategy (100RC pattern)",
        )

    if re.search(r"\bclimate\s+adaptation\s+(plan|strategy|framework)\b", stem):
        # PASS topic; geo is uncertain from filename alone but we flag as PASS
        # since national ones would have been caught above by FAIL_GEO checks
        return Verdict("PASS", "filename", "filename indicates climate adaptation plan")

    if re.search(r"\badaptation\s+(plan|strategy|framework)\b", stem):
        return Verdict(
            "PASS", "filename", "filename indicates adaptation plan/strategy"
        )

    return None  # proceed to stage 2


# ---------------------------------------------------------------------------
# Stage 2: PDF first 3 pages
# ---------------------------------------------------------------------------


def stage2_pdf_pages(pdf_path: Path) -> Verdict | None:
    """Extract first 3 pages and apply keyword checks."""
    text = extract_text(pdf_path, max_pages=3)
    if text.startswith("[extraction error"):
        return None  # try next stage

    text_lower = text.lower()

    # --- Authority: FAIL (academic/corporate indicators) ---
    # Check phrases first
    for phrase in _FAIL_AUTHORITY_PHRASES:
        if _contains_phrase(text_lower, phrase):
            return Verdict(
                "FAIL_AUTHORITY",
                "pdf_pages",
                f"opening pages contain '{phrase}' (non-official document)",
            )

    # Check patterns (DOI, ISSN, academic publishers)
    for pattern in _FAIL_AUTHORITY_PATTERNS:
        if pattern.search(text):
            return Verdict(
                "FAIL_AUTHORITY",
                "pdf_pages",
                f"opening pages match academic/corporate pattern: {pattern.pattern[:50]}",
            )

    # --- Geography: FAIL ---
    for phrase in _FAIL_GEO_PHRASES:
        if _contains_phrase(text_lower, phrase):
            return Verdict("FAIL_GEO", "pdf_pages", f"opening pages contain '{phrase}'")

    # Check for "national" in title-like positions (first 500 chars)
    head = text_lower[:500]
    if _contains_word(head, "national") and not re.search(
        r"\bnational\s+(city|capital|territory)\b", head
    ):
        # Be more conservative: require at least one more signal
        if any(
            _contains_phrase(head, s)
            for s in [
                "adaptation plan",
                "climate plan",
                "climate strategy",
                "resilience strategy",
                "communication",
            ]
        ):
            return Verdict(
                "FAIL_GEO",
                "pdf_pages",
                "title area contains 'national' with plan-type keyword",
            )

    # For multilingual matching, use diacritic-normalized text
    text_norm = _text_lower_norm(text)

    # --- Authority: PASS (official government indicators) ---
    # Check English phrases
    has_official = any(_contains_phrase(text_lower, p) for p in _PASS_AUTHORITY_PHRASES)
    # Check multilingual phrases
    has_official_ml = any(
        _contains_phrase(text_norm, n) for n in _PASS_AUTHORITY_PHRASES_ML_NORM
    )

    # --- Multilingual GEO FAIL in PDF text ---
    for orig, norm in zip(_FAIL_GEO_PHRASES_ML, _FAIL_GEO_PHRASES_ML_NORM):
        if _contains_phrase(text_norm, norm):
            return Verdict(
                "FAIL_GEO",
                "pdf_pages",
                f"opening pages contain multilingual geo-fail phrase '{orig}'",
            )

    # --- Topic: FAIL (only if clear mitigation-only, no adaptation signals) ---
    has_pass_topic = any(
        _contains_phrase(text_lower, p) for p in _PASS_TOPIC_PHRASES
    ) or any(_contains_phrase(text_norm, n) for n in _PASS_TOPIC_PHRASES_ML_NORM)
    has_fail_topic = any(
        _contains_phrase(text_lower, p) for p in _FAIL_TOPIC_PHRASES
    ) or any(_contains_phrase(text_norm, n) for n in _FAIL_TOPIC_PHRASES_ML_NORM)

    if has_fail_topic and not has_pass_topic:
        matched = next(
            (p for p in _FAIL_TOPIC_PHRASES if _contains_phrase(text_lower, p)), None
        ) or next(
            (
                o
                for o, n in zip(_FAIL_TOPIC_PHRASES_ML, _FAIL_TOPIC_PHRASES_ML_NORM)
                if _contains_phrase(text_norm, n)
            ),
            "unknown phrase",
        )
        return Verdict(
            "FAIL_TOPIC",
            "pdf_pages",
            f"opening pages indicate mitigation/sustainability focus ('{matched}'), no adaptation signals",
        )

    # --- PASS: clear adaptation focus AND official government source ---
    if has_pass_topic and (has_official or has_official_ml):
        phrase = next(
            (p for p in _PASS_TOPIC_PHRASES if _contains_phrase(text_lower, p)), None
        ) or next(
            (
                o
                for o, n in zip(_PASS_TOPIC_PHRASES_ML, _PASS_TOPIC_PHRASES_ML_NORM)
                if _contains_phrase(text_norm, n)
            ),
            "multilingual adaptation keyword",
        )
        return Verdict(
            "PASS",
            "pdf_pages",
            f"opening pages show adaptation/resilience focus ('{phrase}') + official government source",
        )

    # --- PASS: clear adaptation focus (without explicit government signal, but no academic markers) ---
    if has_pass_topic:
        phrase = next(
            (p for p in _PASS_TOPIC_PHRASES if _contains_phrase(text_lower, p)), None
        ) or next(
            (
                o
                for o, n in zip(_PASS_TOPIC_PHRASES_ML, _PASS_TOPIC_PHRASES_ML_NORM)
                if _contains_phrase(text_norm, n)
            ),
            "multilingual adaptation keyword",
        )
        return Verdict(
            "PASS",
            "pdf_pages",
            f"opening pages show adaptation/resilience focus ('{phrase}')",
        )

    return None  # proceed to stage 3


# ---------------------------------------------------------------------------
# Stage 3: Full text scan (first 20 pages)
# ---------------------------------------------------------------------------


def stage3_full_text(pdf_path: Path) -> Verdict | None:
    """Keyword density analysis and chapter heading detection."""
    text = extract_text(pdf_path, max_pages=20)
    if text.startswith("[extraction error"):
        return None

    text_lower = text.lower()
    text_norm = _text_lower_norm(text)

    # --- Authority: FAIL (academic/corporate patterns) ---
    # Count academic markers: references/bibliography/citations sections, academic terminology
    academic_markers = 0
    if re.search(r"\b(references|bibliography|works cited)\s*\n", text_lower):
        academic_markers += 1
    if re.search(r"\b(abstract|keywords)\s*:\s*\w+", text_lower):
        academic_markers += 1
    for pattern in _FAIL_AUTHORITY_PATTERNS:
        if pattern.search(text):
            academic_markers += 1

    # Check for repeated academic phrases
    for phrase in _FAIL_AUTHORITY_PHRASES:
        if _contains_phrase(text_lower, phrase):
            academic_markers += 1

    # Multiple academic markers = likely not an official plan
    if academic_markers >= 3:
        return Verdict(
            "FAIL_AUTHORITY",
            "full_text",
            f"document shows {academic_markers} academic/non-official markers",
        )

    # --- Geography: national/state indicators ---
    for phrase in _FAIL_GEO_PHRASES:
        if _contains_phrase(text_lower, phrase):
            return Verdict("FAIL_GEO", "full_text", f"full text contains '{phrase}'")
    for orig, norm in zip(_FAIL_GEO_PHRASES_ML, _FAIL_GEO_PHRASES_ML_NORM):
        if _contains_phrase(text_norm, norm):
            return Verdict(
                "FAIL_GEO",
                "full_text",
                f"full text contains multilingual phrase '{orig}'",
            )

    # Check for repeated "national" in context (body text, not just passing references)
    national_hits = len(
        re.findall(
            r"\bnational\s+(plan|strategy|framework|program|programme|policy)\b",
            text_lower,
        )
    )
    if national_hits >= 3:
        return Verdict(
            "FAIL_GEO",
            "full_text",
            f"full text references 'national plan/strategy/framework' {national_hits} times",
        )

    # --- Topic: vocabulary density analysis ---
    adapt_count = len(_ADAPT_VOCAB.findall(text))
    mitig_count = len(_MITIG_VOCAB.findall(text))
    total = adapt_count + mitig_count

    # Strong adaptation signal even with low absolute counts
    if adapt_count >= 10 and mitig_count <= 2:
        return Verdict(
            "PASS",
            "full_text",
            f"adaptation vocabulary clear: {adapt_count} adapt terms, only {mitig_count} mitig",
        )

    # Strong mitigation signal with near-zero adaptation
    if mitig_count >= 15 and adapt_count <= 3:
        for pat in _RESILIENCE_CHAPTER_PATTERNS:
            if pat.search(text):
                return Verdict(
                    "PASS",
                    "full_text",
                    "mitigation-focused document but contains a resilience/adaptation chapter heading",
                )
        return Verdict(
            "FAIL_TOPIC",
            "full_text",
            f"mitigation vocabulary clear: {mitig_count} mitig terms, only {adapt_count} adapt",
        )

    if total > 20:
        adapt_ratio = adapt_count / total
        if adapt_ratio >= 0.35:
            return Verdict(
                "PASS",
                "full_text",
                f"adaptation vocabulary dominates: {adapt_count} adapt vs {mitig_count} mitig terms "
                f"({adapt_ratio:.0%} adapt)",
            )
        if adapt_ratio < 0.15:
            for pat in _RESILIENCE_CHAPTER_PATTERNS:
                if pat.search(text):
                    return Verdict(
                        "PASS",
                        "full_text",
                        "mitigation-focused document but contains a resilience/adaptation chapter heading",
                    )
            return Verdict(
                "FAIL_TOPIC",
                "full_text",
                f"mitigation vocabulary dominates: {mitig_count} mitig vs {adapt_count} adapt terms "
                f"({1 - adapt_ratio:.0%} mitig); no resilience chapter found",
            )

    # Low vocabulary count: document is probably non-English, scanned, or off-topic
    # Send to LLM for analysis
    return None


# ---------------------------------------------------------------------------
# Stage 4: LLM (claude-haiku)
# ---------------------------------------------------------------------------

_LLM_PROMPT_TEMPLATE = """\
You are reviewing a climate-related planning document to determine if it belongs \
in a database of city-level climate adaptation plans.

Filename: {filename}
Detected language: {lang}

Evaluate THREE criteria:

CRITERION A — Geographic scope:
- PASS: plan for a city, town, municipality, county, borough, district, metropolitan area, or sub-national unit
- FAIL: national, federal, state, provincial, regional, or country-level plan

CRITERION B — Topic focus:
- PASS: primarily climate adaptation, climate resilience, vulnerability, or hazard/disaster risk reduction
  - Also PASS: broader plan that contains a SUBSTANTIAL section (a full chapter, 15+ pages) on resilience/adaptation
- FAIL: primarily GHG mitigation, emissions reduction, net-zero, decarbonization, or sustainability WITHOUT a substantial resilience section

CRITERION C — Authority/Source:
- PASS: official government document (city council, municipal government, planning department, mayor's office)
  - Look for: adoption by city council, official plan designation, government department authorship
- FAIL: academic paper, white paper, working paper, consultant report, corporate document, policy brief
  - Look for: university affiliation, journal publication, DOI/ISSN, "prepared by" consultant, literature review

Respond in JSON only (no markdown fences):
{{
  "geo_scope": "city" | "state" | "national" | "unclear",
  "topic_focus": "adaptation" | "mitigation" | "mixed_with_adaptation" | "mixed_without_adaptation" | "unclear",
  "authority": "official_government" | "academic" | "consultant" | "corporate" | "unclear",
  "verdict": "PASS" | "FAIL_GEO" | "FAIL_TOPIC" | "FAIL_AUTHORITY" | "FAIL_MULTIPLE" | "HUMAN_REVIEW",
  "reasoning": "<2-3 sentences covering all three criteria>"
}}

Document text (first ~15 pages):
---
{text}
---"""


def stage4_llm(
    pdf_path: Path, llm_client, llm_calls_counter: list[int], llm_limit: int
) -> Verdict:
    """Send first 15 pages to the configured LLM for classification."""
    if llm_calls_counter[0] >= llm_limit:
        return Verdict(
            "HUMAN_REVIEW",
            "llm",
            f"LLM call limit ({llm_limit}) reached; cannot auto-classify",
        )

    text = extract_text(pdf_path, max_pages=15)
    sidecar = load_sidecar(pdf_path.with_suffix(pdf_path.suffix + ".json"))
    lang_hint = sidecar.get("language", "unknown")

    prompt = _LLM_PROMPT_TEMPLATE.format(
        filename=pdf_path.name,
        lang=lang_hint,
        text=text[:60000],
    )

    try:
        provider = getattr(llm_client, "_provider", "anthropic")

        if provider == "openai":
            response = llm_client.chat.completions.create(
                model=llm_client._triage_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.choices[0].message.content.strip()
        else:
            response = llm_client.messages.create(
                model=llm_client._triage_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

        llm_calls_counter[0] += 1
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

        result = json.loads(raw)
        verdict_str = result.get("verdict", "HUMAN_REVIEW")
        reasoning = result.get("reasoning", "LLM provided no reasoning")

        # Normalize FAIL_BOTH and FAIL_MULTIPLE to the specific failure type
        # (or keep as HUMAN_REVIEW if truly ambiguous)
        if verdict_str not in (
            "PASS",
            "FAIL_GEO",
            "FAIL_TOPIC",
            "FAIL_AUTHORITY",
            "FAIL_BOTH",
            "FAIL_MULTIPLE",
            "HUMAN_REVIEW",
        ):
            verdict_str = "HUMAN_REVIEW"

        return Verdict(verdict_str, "llm", f"[LLM] {reasoning}")

    except json.JSONDecodeError as exc:
        return Verdict("HUMAN_REVIEW", "llm", f"LLM returned non-JSON response: {exc}")
    except Exception as exc:
        return Verdict("HUMAN_REVIEW", "llm", f"LLM call failed: {exc}")


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def classify(
    pdf_path: Path, llm_client, llm_calls_counter: list[int], llm_limit: int
) -> Verdict:
    verdict = stage1_filename(pdf_path)
    if verdict:
        return verdict

    verdict = stage2_pdf_pages(pdf_path)
    if verdict:
        return verdict

    verdict = stage3_full_text(pdf_path)
    if verdict:
        return verdict

    if llm_client:
        return stage4_llm(pdf_path, llm_client, llm_calls_counter, llm_limit)

    return Verdict(
        "HUMAN_REVIEW",
        "full_text",
        "Could not classify automatically; no LLM available",
    )


def move_with_sidecar(pdf_path: Path, dest_dir: Path, dry_run: bool) -> None:
    """Move PDF and its sidecar to dest_dir, preserving ISO3/ID subfolder structure."""
    # pdf_path is like plans/_incoming_for_review/ISO3/ID/file.pdf
    # We want dest_dir/ISO3/ID/file.pdf (stripping _incoming_for_review if present)
    try:
        rel = pdf_path.relative_to(PLANS_DIR)
        # Strip _incoming_for_review prefix if present
        if rel.parts[0] == "_incoming_for_review" and len(rel.parts) > 1:
            rel = Path(*rel.parts[1:])
    except ValueError:
        rel = Path(pdf_path.name)

    dest = dest_dir / rel
    sidecar = pdf_path.with_suffix(pdf_path.suffix + ".json")
    dest_sidecar = dest.with_suffix(dest.suffix + ".json")

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf_path), str(dest))
        if sidecar.exists():
            shutil.move(str(sidecar), str(dest_sidecar))
    else:
        print(f"  [dry-run] would move {pdf_path.name} → {dest}")


def process_pdf(
    pdf_path: Path,
    llm_client,
    llm_calls_counter: list[int],
    llm_limit: int,
    dry_run: bool,
    force: bool,
) -> str:
    """Classify, update sidecar, and move if needed. Returns status string."""
    sidecar_path = pdf_path.with_suffix(pdf_path.suffix + ".json")
    sidecar = load_sidecar(sidecar_path)

    # Skip already-triaged unless --force
    if not force and "triage_verdict" in sidecar:
        return f"skip:{sidecar['triage_verdict']}"

    verdict = classify(pdf_path, llm_client, llm_calls_counter, llm_limit)

    # Update sidecar
    sidecar["triage_verdict"] = verdict.status
    sidecar["triage_stage"] = verdict.stage
    sidecar["triage_reason"] = verdict.reason
    sidecar["triage_date"] = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        # Write sidecar before moving (move will carry it along)
        save_sidecar(sidecar_path, sidecar)

        if verdict.status in (
            "FAIL_GEO",
            "FAIL_TOPIC",
            "FAIL_AUTHORITY",
            "FAIL_BOTH",
            "FAIL_MULTIPLE",
        ):
            move_with_sidecar(pdf_path, REJECTED_DIR, dry_run=False)
        elif verdict.status == "HUMAN_REVIEW":
            move_with_sidecar(pdf_path, HUMAN_REVIEW_DIR, dry_run=False)
        # PASS: leave in place
    else:
        if verdict.status in (
            "FAIL_GEO",
            "FAIL_TOPIC",
            "FAIL_AUTHORITY",
            "FAIL_BOTH",
            "FAIL_MULTIPLE",
        ):
            print(f"  [dry-run] FAIL → _rejected/  ({verdict.reason})")
        elif verdict.status == "HUMAN_REVIEW":
            print(
                f"  [dry-run] HUMAN_REVIEW → _human_review_needed/  ({verdict.reason})"
            )
        else:
            print(f"  [dry-run] PASS ({verdict.reason})")

    return verdict.status


def find_pdfs() -> list[Path]:
    """Find all PDFs in plans/, excluding _rejected and _human_review_needed."""
    exclude = {REJECTED_DIR, HUMAN_REVIEW_DIR}
    pdfs = []
    for p in PLANS_DIR.rglob("*.pdf"):
        if not any(p.is_relative_to(excl) for excl in exclude):
            pdfs.append(p)
    return sorted(pdfs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage climate adaptation plans")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without moving files",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-triage even if already classified"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM stage (unresolved → HUMAN_REVIEW)",
    )
    parser.add_argument(
        "--llm-limit",
        type=int,
        default=200,
        help="Max number of LLM calls (default: 200)",
    )
    parser.add_argument(
        "--model",
        default="auto",
        help="Model for LLM stage. 'auto' uses LLM_MODEL from .env "
        "(default: auto → google.gemini-3.1-flash-lite-preview or claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--sample", type=int, default=0, help="Process only first N files (for testing)"
    )
    args = parser.parse_args()

    # Set up LLM client — loads .env, tries OpenAI-compatible proxy first, then Anthropic
    llm_client = None
    if not args.no_llm:
        import os
        from dotenv import load_dotenv

        env_path = Path(__file__).parent.parent / ".env"
        load_dotenv(env_path, override=False)

        # Determine model: CLI arg > LLM_MODEL env > default
        model = (
            args.model
            if args.model != "auto"
            else (os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"))
        )

        # Try OpenAI-compatible (LiteLLM proxy, Cornell, etc.)
        base_url = os.getenv("OPENAI_API_BASE") or os.getenv("LITELLM_PROXY_URL")
        openai_key = os.getenv("OPENAI_API_KEY")
        if base_url and openai_key:
            try:
                import openai as _openai

                client = _openai.OpenAI(base_url=base_url, api_key=openai_key)
                client._triage_model = model
                client._provider = "openai"
                llm_client = client
                print(f"LLM: OpenAI-compatible proxy at {base_url}, model={model}")
            except Exception as exc:
                print(f"Warning: could not init OpenAI client: {exc}", file=sys.stderr)

        # Fall back to Anthropic SDK
        if llm_client is None:
            anthropic_key = os.getenv("ANTHROPIC_API_KEY")
            if anthropic_key:
                try:
                    import anthropic as _anthropic

                    client = _anthropic.Anthropic(api_key=anthropic_key)
                    client._triage_model = model
                    client._provider = "anthropic"
                    llm_client = client
                    print(f"LLM: Anthropic SDK, model={model}")
                except Exception as exc:
                    print(
                        f"Warning: could not init Anthropic client: {exc}",
                        file=sys.stderr,
                    )

        if llm_client is None:
            print(
                "Warning: no LLM credentials found; LLM stage disabled. "
                "Set OPENAI_API_KEY+OPENAI_API_BASE or ANTHROPIC_API_KEY.",
                file=sys.stderr,
            )

    pdfs = find_pdfs()
    if args.sample:
        pdfs = pdfs[: args.sample]

    print(f"Found {len(pdfs)} PDFs to process")
    if args.dry_run:
        print("DRY RUN — no files will be moved")
    print()

    if not args.dry_run:
        REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        HUMAN_REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    llm_calls_counter = [0]
    counts: dict[str, int] = {}

    for pdf in tqdm(pdfs, unit="pdf", ncols=90):
        status = process_pdf(
            pdf,
            llm_client=llm_client,
            llm_calls_counter=llm_calls_counter,
            llm_limit=args.llm_limit,
            dry_run=args.dry_run,
            force=args.force,
        )
        bucket = status.split(":")[0] if status.startswith("skip:") else status
        counts[bucket] = counts.get(bucket, 0) + 1

    print()
    print("=== Results ===")
    for status in sorted(counts):
        print(f"  {status:<20} {counts[status]:>5}")
    print(f"  {'LLM calls':<20} {llm_calls_counter[0]:>5}")


if __name__ == "__main__":
    main()
