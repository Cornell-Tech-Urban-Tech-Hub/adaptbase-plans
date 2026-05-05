#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pypdf",
#   "pymupdf",
#   "langdetect",
# ]
# ///
"""
Diagnose PDFs whose language was recorded as "unknown".

Read-only. Walks plans/ and plans/_incoming_for_review/, finds every metadata
sidecar with language == "unknown", and re-runs text extraction with two
extractors over a wider page window so we can see what would resolve them:

  * pypdf  (current extractor) — over up to PAGE_CAP pages
  * fitz   (PyMuPDF)           — over up to PAGE_CAP pages

For each PDF we record character counts, the language each extractor's text
detects to (or the error), and a verdict bucket. Results are written to
scripts/unknown_language_diagnostic.csv plus a summary on stdout.

Nothing is modified. Use this output to decide whether switching extractor,
raising the page cap, or adding OCR is the right next step.
"""

import argparse
import csv
import json
import logging
from pathlib import Path

import pymupdf  # PyMuPDF
from langdetect import DetectorFactory, LangDetectException, detect
from pypdf import PdfReader

DetectorFactory.seed = 0
logging.getLogger("pypdf").setLevel(logging.ERROR)

PAGE_CAP = 20  # pages to try per PDF (vs. 5 in the current backfill script)
MIN_CHARS = 50
SAMPLE_CHARS = 2000


def _detect(text: str) -> tuple[str, str]:
    """Return (language, error). Empty error means success."""
    text = text.strip()
    if len(text) < MIN_CHARS:
        return "", f"too_short ({len(text)} chars)"
    try:
        return detect(text[:SAMPLE_CHARS]), ""
    except LangDetectException as e:
        return "", f"langdetect: {e}"


def extract_pypdf(pdf_path: Path) -> tuple[str, int, str]:
    """Returns (text, page_count, error)."""
    try:
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        chunks = []
        for i in range(min(PAGE_CAP, page_count)):
            chunks.append(reader.pages[i].extract_text() or "")
        return " ".join(chunks).strip(), page_count, ""
    except Exception as e:
        return "", 0, f"pypdf: {type(e).__name__}: {e}"


def extract_fitz(pdf_path: Path) -> tuple[str, int, str]:
    """Returns (text, page_count, error)."""
    try:
        with pymupdf.open(pdf_path) as doc:
            page_count = doc.page_count
            chunks = []
            for i in range(min(PAGE_CAP, page_count)):
                chunks.append(doc[i].get_text() or "")
            return " ".join(chunks).strip(), page_count, ""
    except Exception as e:
        return "", 0, f"fitz: {type(e).__name__}: {e}"


def find_unknown_pdfs(roots: list[Path]) -> list[Path]:
    """Return PDF paths whose sidecar JSON has language == 'unknown'."""
    pdfs = []
    for root in roots:
        if not root.exists():
            continue
        for meta_path in root.rglob("*.pdf.json"):
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                continue
            if meta.get("language") == "unknown":
                pdf_path = meta_path.with_suffix("")  # strip .json -> .pdf
                if pdf_path.exists():
                    pdfs.append(pdf_path)
    return sorted(pdfs)


def verdict(pypdf_lang: str, fitz_lang: str) -> str:
    if pypdf_lang and fitz_lang:
        return "resolved_consistent" if pypdf_lang == fitz_lang else "resolved_disagree"
    if fitz_lang and not pypdf_lang:
        return "resolved_by_fitz"
    if pypdf_lang and not fitz_lang:
        return "resolved_by_pypdf"
    return "still_unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="scripts/unknown_language_diagnostic.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    roots = [Path("plans"), Path("plans") / "_incoming_for_review"]
    pdfs = find_unknown_pdfs(roots)
    print(f"Found {len(pdfs)} PDFs with language='unknown'\n")
    if not pdfs:
        return

    rows = []
    counts = {
        "resolved_consistent": 0,
        "resolved_disagree": 0,
        "resolved_by_fitz": 0,
        "resolved_by_pypdf": 0,
        "still_unknown": 0,
    }
    by_fitz_lang: dict[str, int] = {}

    for i, pdf in enumerate(pdfs, 1):
        rel = pdf.relative_to(Path.cwd()) if pdf.is_absolute() else pdf
        country = pdf.parts[-3] if len(pdf.parts) >= 3 else ""

        py_text, py_pages, py_err = extract_pypdf(pdf)
        fz_text, fz_pages, fz_err = extract_fitz(pdf)

        py_lang, py_lang_err = _detect(py_text) if not py_err else ("", py_err)
        fz_lang, fz_lang_err = _detect(fz_text) if not fz_err else ("", fz_err)

        v = verdict(py_lang, fz_lang)
        counts[v] += 1
        if fz_lang:
            by_fitz_lang[fz_lang] = by_fitz_lang.get(fz_lang, 0) + 1

        rows.append(
            {
                "path": str(rel),
                "country": country,
                "pages": fz_pages or py_pages,
                "pypdf_chars": len(py_text),
                "pypdf_lang": py_lang,
                "pypdf_error": py_lang_err,
                "fitz_chars": len(fz_text),
                "fitz_lang": fz_lang,
                "fitz_error": fz_lang_err,
                "verdict": v,
            }
        )

        marker = {
            "resolved_consistent": "✓",
            "resolved_disagree": "?",
            "resolved_by_fitz": "F",
            "resolved_by_pypdf": "P",
            "still_unknown": "✗",
        }[v]
        print(
            f"[{i:>3}/{len(pdfs)}] {marker} "
            f"py={len(py_text):>6}/{py_lang or '-':<6} "
            f"fz={len(fz_text):>6}/{fz_lang or '-':<6}  {rel}"
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 60)
    print(f"Total unknown PDFs: {len(pdfs)}")
    for k, n in counts.items():
        print(f"  {k:<22} {n:>4}")
    print(f"\nCSV written to: {out_path}")
    if by_fitz_lang:
        print("\nLanguages fitz would assign (where it succeeded):")
        for lang, n in sorted(by_fitz_lang.items(), key=lambda kv: -kv[1]):
            print(f"  {lang:<6} {n}")
    print("\nVerdict legend:")
    print("  resolved_consistent  pypdf and fitz agree on language")
    print("  resolved_disagree    both extracted, but disagree (inspect)")
    print("  resolved_by_fitz     only fitz produced detectable text")
    print("  resolved_by_pypdf    only pypdf produced detectable text")
    print(
        "  still_unknown        neither extractor produced enough text — likely scan/OCR territory"
    )


if __name__ == "__main__":
    main()
