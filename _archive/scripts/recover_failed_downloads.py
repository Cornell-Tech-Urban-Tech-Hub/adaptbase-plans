#!/usr/bin/env python3
# /// script
# dependencies = [
#   "httpx",
#   "curl_cffi",
#   "pymupdf",
#   "langdetect",
#   "beautifulsoup4",
#   "lxml",
#   "python-dotenv",
# ]
# ///
"""
Recover failed downloads from scripts/failed_downloads.json.

Runs each failing URL through a strategy ladder until one succeeds:

  1. curl_cffi w/ Chrome/Safari/Edge TLS-fingerprint impersonation
     (defeats Cloudflare bot challenges that block plain Python TLS)
  2. httpx with browser headers + Referer (rotating User-Agents)
  3. httpx with SSL verification disabled
  4. curl subprocess (Mac SecureTransport differs from Python TLS)
  5. http:// fallback when the original was https://
  6. Wayback Machine snapshot (id_/ raw object)
  7. HTML scrape: parse any HTML response for a PDF link, recurse

Successful downloads land in:
    plans/_incoming/{ISO3}/{loc_id}/<filename>.pdf
    plans/_incoming/{ISO3}/{loc_id}/<filename>.json   (metadata + recovery info)

Resumable: progress saved to scripts/recovery_progress.json after each URL.
Still-failing URLs are written to scripts/recovery_still_failed.json.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import pymupdf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langdetect import DetectorFactory, LangDetectException, detect

try:
    from curl_cffi import requests as cffi_requests  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    cffi_requests = None  # type: ignore[assignment]

load_dotenv()
DetectorFactory.seed = 0

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_PATH = Path.cwd()
INCOMING_DIR = BASE_PATH / "plans" / "_incoming"
EXISTING_REVIEW_DIR = BASE_PATH / "plans" / "_incoming_for_review"
PLANS_DIR = BASE_PATH / "plans"
FAILED_FILE = BASE_PATH / "scripts" / "failed_downloads.json"
PROGRESS_FILE = BASE_PATH / "scripts" / "recovery_progress.json"
STILL_FAILED_FILE = BASE_PATH / "scripts" / "recovery_still_failed.json"

LANGUAGE_PAGE_CAP = 20
UNKNOWN_LANG_BUCKET = "_unknown_language"

# Hosts that need auth/JS and are not worth retrying
HARD_SKIP_HOSTS = {
    "www.instagram.com",
    "instagram.com",
    "www.facebook.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "www.researchgate.net",
    "researchgate.net",
    "www.scribd.com",
    "scribd.com",
    "www.linkedin.com",
    "linkedin.com",
    # Gov servers that accept TCP but never send data
    "upsdma.up.nic.in",
}

USER_AGENTS = [
    # Chrome on macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    # Firefox on Linux
    ("Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"),
    # Edge on Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    # Googlebot
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
    # Bingbot
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"),
]

PDF_CONTENT_TYPES = (
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _is_pdf_bytes(data: bytes) -> bool:
    return bool(data) and data[:5] == b"%PDF-"


def _content_type_is_pdf(content_type: str) -> bool:
    return any(t in (content_type or "").lower() for t in PDF_CONTENT_TYPES)


def _detect_language(pdf_bytes: bytes) -> str:
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count == 0:
                return "unknown"
            chunks = [
                doc[i].get_text() or ""
                for i in range(min(LANGUAGE_PAGE_CAP, doc.page_count))
            ]
            text = " ".join(chunks).strip()
            if not text:
                return "unknown"
            try:
                return detect(text[:1000])
            except LangDetectException:
                return "unknown"
    except Exception:
        return "unknown"


def _sanitize_filename(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in "._- ")
    cleaned = cleaned.strip().replace(" ", "_")
    if not cleaned:
        cleaned = "recovered_plan"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    if len(cleaned) > 180:
        cleaned = cleaned[:175] + ".pdf"
    return cleaned


def _filename_from_url(url: str, fallback: str) -> str:
    path = urlparse(url).path
    last = path.rsplit("/", 1)[-1] if path else ""
    if last and last.lower().endswith(".pdf"):
        return _sanitize_filename(last)
    return _sanitize_filename(fallback)


# ---------------------------------------------------------------------------
# Existing-file checks (mirror logic from auto_search_plans_gemini_v2.py)
# ---------------------------------------------------------------------------


def _folder_has_url(folder: Path, url: str) -> bool:
    if not folder.exists():
        return False
    for meta in folder.glob("*.json"):
        try:
            data = json.loads(meta.read_text())
            if data.get("url") == url:
                return True
        except Exception:
            continue
    return False


def _folder_has_filename(folder: Path, filename: str) -> bool:
    if not folder.exists():
        return False
    if (folder / filename).exists():
        return True
    base = Path(filename).stem.lower()
    return any(p.stem.lower() == base for p in folder.glob("*.pdf"))


def already_downloaded(city: dict, url: str, filename: str) -> tuple[bool, str]:
    """Check plans/, _incoming_for_review/ (and unknown-lang bucket), and _incoming/."""
    iso3 = city["country_code_iso3"]
    loc_id = str(city["loc_id"])

    candidates = [
        ("plans", PLANS_DIR / iso3 / loc_id),
        ("incoming_for_review", EXISTING_REVIEW_DIR / iso3 / loc_id),
        (
            "incoming_for_review_unknown",
            EXISTING_REVIEW_DIR / UNKNOWN_LANG_BUCKET / iso3 / loc_id,
        ),
        ("incoming", INCOMING_DIR / iso3 / loc_id),
    ]
    for label, folder in candidates:
        if _folder_has_url(folder, url):
            return True, label
        if _folder_has_filename(folder, filename):
            return True, label
    return False, ""


# ---------------------------------------------------------------------------
# Download strategies
# ---------------------------------------------------------------------------


def _build_headers(url: str, ua: str, accept_pdf: bool = False) -> dict:
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    accept = (
        "application/pdf,application/octet-stream,*/*;q=0.8"
        if accept_pdf
        else (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf;q=0.9,*/*;q=0.8"
        )
    )
    return {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
    }


CFFI_IMPERSONATIONS = ("chrome124", "safari17_0")


def _try_cffi(url: str, timeout: int = 8) -> tuple[bytes | None, str, str]:
    """Use curl_cffi to mimic a real browser's TLS fingerprint (defeats Cloudflare).

    Returns (bytes_or_None, content_type, error).
    """
    if cffi_requests is None:
        return None, "", "curl_cffi not installed"

    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
    }

    last_err = ""
    for impersonation in CFFI_IMPERSONATIONS:
        try:
            r = cffi_requests.get(
                url,
                impersonate=impersonation,
                timeout=timeout,
                allow_redirects=True,
                headers=headers,
                verify=True,
            )
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code} ({impersonation})"
                continue
            return r.content, r.headers.get("content-type", ""), ""
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:120]} ({impersonation})"
            continue
    return None, "", last_err or "all curl_cffi impersonations failed"


def _try_httpx(
    url: str, timeout: float = 8.0, verify: bool = True
) -> tuple[bytes | None, str, str]:
    """Try httpx with a couple of User-Agents. (bytes_or_None, content_type, error)."""
    last_err = ""
    # Only two UAs — Chrome and Googlebot — to keep worst-case time bounded.
    for ua in (USER_AGENTS[0], USER_AGENTS[3]):
        try:
            with httpx.Client(
                headers=_build_headers(url, ua, accept_pdf=False),
                timeout=httpx.Timeout(timeout, connect=5.0),
                follow_redirects=True,
                verify=verify,
                http2=False,
            ) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    continue
                return resp.content, resp.headers.get("content-type", ""), ""
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue
    return None, "", last_err or "all UA attempts failed"


def _try_curl(
    url: str, timeout: int = 8, insecure: bool = False
) -> tuple[bytes | None, str, str]:
    """Use system curl. Returns (bytes_or_None, content_type, error)."""
    cmd = [
        "curl",
        "-sSL",
        "--max-time",
        str(timeout),
        "--connect-timeout",
        "5",
        "-A",
        USER_AGENTS[0],
        "-H",
        "Accept: application/pdf,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        f"Referer: {urlparse(url).scheme}://{urlparse(url).netloc}/",
        "-w",
        "\n%{content_type}",  # append content-type at end
    ]
    if insecure:
        cmd.append("-k")
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if proc.returncode != 0:
            stderr_excerpt = proc.stderr.decode(errors="replace")[:200]
            return None, "", f"curl exit {proc.returncode}: {stderr_excerpt}"
        body = proc.stdout
        # split off the trailing content-type we asked curl to print
        nl = body.rfind(b"\n")
        if nl > 0:
            content_type = body[nl + 1 :].decode(errors="replace").strip()
            body = body[:nl]
        else:
            content_type = ""
        return body, content_type, ""
    except subprocess.TimeoutExpired:
        return None, "", "curl timeout"
    except FileNotFoundError:
        return None, "", "curl not installed"
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}"


def _scrape_pdf_link(html: bytes, base_url: str) -> str | None:
    """Look at an HTML page, return the most plausible embedded PDF URL or None."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return None

    candidates: list[tuple[int, str]] = []

    def add(score: int, url: str | None) -> None:
        if not url:
            return
        absolute = urljoin(base_url, url)
        if absolute.startswith(("http://", "https://")):
            candidates.append((score, absolute))

    # strongest signals first
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        text = (tag.get_text() or "").lower()
        if href.lower().endswith(".pdf"):
            add(100, href)
        elif ".pdf" in href.lower():
            add(80, href)
        elif "download" in text or "descargar" in text or "télécharger" in text:
            add(40, href)

    for tag in soup.find_all(["iframe", "embed"], src=True):
        src = tag["src"]
        if ".pdf" in src.lower():
            add(90, src)
    for tag in soup.find_all("object", data=True):
        if ".pdf" in tag["data"].lower():
            add(90, tag["data"])

    # meta refresh and og:url, sometimes used by document viewers
    for meta in soup.find_all("meta"):
        if meta.get("http-equiv", "").lower() == "refresh":
            content = meta.get("content", "")
            m = re.search(r"url=([^;\s]+)", content, flags=re.IGNORECASE)
            if m:
                add(60, m.group(1))

    # JS-encoded PDF URLs are common on government sites
    text_blob = html.decode("utf-8", errors="replace")
    for m in re.finditer(
        r"""['"]([^'"\s]+\.pdf)(?:\?[^'"\s]*)?['"]""", text_blob, flags=re.IGNORECASE
    ):
        add(50, m.group(1))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    # de-dupe but keep order
    seen: set[str] = set()
    for _score, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        return url
    return None


def _try_wayback(url: str) -> tuple[bytes | None, str, str]:
    """Look up the URL in the Wayback Machine and try to download the snapshot."""
    api = "https://archive.org/wayback/available"
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            r = client.get(api, params={"url": url})
            if r.status_code != 200:
                return None, "", f"wayback api status {r.status_code}"
            data = r.json()
            snap = data.get("archived_snapshots", {}).get("closest", {})
            if not snap or not snap.get("available"):
                return None, "", "no wayback snapshot"
            snap_url = snap.get("url")
            if not snap_url:
                return None, "", "wayback snapshot missing url"
            # Use id_ flag to get the original raw object instead of a wrapped page.
            snap_url = re.sub(r"/web/(\d+)/", r"/web/\1id_/", snap_url)
            # Try cffi first, fall back to httpx — archive.org occasionally
            # serves snapshots through CDNs that flag plain Python.
            body, ct, _err = _try_cffi(snap_url, timeout=8)
            if body is not None:
                return body, ct, ""
            return _try_httpx(snap_url, timeout=8.0)
    except Exception as e:  # noqa: BLE001
        return None, "", f"wayback error: {type(e).__name__}: {e}"


def _try_http_fallback(url: str) -> tuple[bytes | None, str, str]:
    if not url.startswith("https://"):
        return None, "", "not https"
    return _try_httpx("http://" + url[len("https://") :], timeout=8.0)


# ---------------------------------------------------------------------------
# Strategy ladder
# ---------------------------------------------------------------------------


class _HardDeadlineError(Exception):
    """Raised by SIGALRM to abort a hung URL."""


def _alarm_handler(_signum, _frame):  # noqa: ANN001
    raise _HardDeadlineError("per-URL deadline exceeded")


@contextlib.contextmanager
def hard_deadline(seconds: int):
    """Context manager that raises _HardDeadlineError if the body exceeds `seconds`.

    Uses SIGALRM — Unix only, must run on the main thread.
    """
    prev = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def recover_url(url: str, depth: int = 0) -> tuple[bytes | None, str, str]:
    """Run the strategy ladder. Returns (pdf_bytes_or_None, recovered_via, error)."""
    if depth > 1:
        return None, "", "scrape recursion exceeded"

    attempts: list[tuple[str, callable]] = [
        ("curl_cffi", lambda: _try_cffi(url, timeout=8)),
        ("direct_httpx", lambda: _try_httpx(url, timeout=8.0, verify=True)),
        ("httpx_no_ssl_verify", lambda: _try_httpx(url, timeout=8.0, verify=False)),
        ("curl", lambda: _try_curl(url, timeout=8, insecure=False)),
        ("curl_insecure", lambda: _try_curl(url, timeout=8, insecure=True)),
        ("http_fallback", lambda: _try_http_fallback(url)),
        ("wayback", lambda: _try_wayback(url)),
    ]

    last_html: bytes | None = None
    last_html_err = ""

    for name, fn in attempts:
        body, content_type, err = fn()
        if body is None:
            continue
        if _is_pdf_bytes(body) or _content_type_is_pdf(content_type):
            return body, name, ""
        # got HTML — remember it for scrape attempt
        if last_html is None and body:
            last_html = body
            last_html_err = f"{name} returned non-PDF ({content_type or 'unknown'})"

    # Last resort: scrape the HTML for a PDF link, then recurse on that link.
    if last_html is not None:
        scraped = _scrape_pdf_link(last_html, url)
        if scraped and scraped != url:
            body, via, err = recover_url(scraped, depth=depth + 1)
            if body is not None:
                return body, f"html_scrape({via})", ""
            return None, "", f"html scrape found {scraped} but recovery failed: {err}"

    return None, "", last_html_err or "all strategies failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {
        "last_updated": None,
        "total": 0,
        "recovered": 0,
        "skipped_existing": 0,
        "skipped_hard": 0,
        "still_failed": 0,
        "urls_processed": {},
    }


def save_progress(progress: dict) -> None:
    progress["last_updated"] = _now_iso()
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=2))
    tmp.replace(PROGRESS_FILE)


def save_still_failed(records: list[dict]) -> None:
    tmp = STILL_FAILED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2))
    tmp.replace(STILL_FAILED_FILE)


def _save_pdf(
    pdf_bytes: bytes,
    city: dict,
    filename: str,
    original: dict,
    recovered_via: str,
    final_url: str,
) -> Path:
    iso3 = city["country_code_iso3"]
    loc_id = str(city["loc_id"])
    folder = INCOMING_DIR / iso3 / loc_id
    folder.mkdir(parents=True, exist_ok=True)

    dest = folder / filename
    if dest.exists():
        base = dest.stem
        ext = dest.suffix
        n = 1
        while dest.exists():
            dest = folder / f"{base}_recovered_{n}{ext}"
            n += 1

    dest.write_bytes(pdf_bytes)

    language = _detect_language(pdf_bytes)

    meta = {
        "url": original.get("url"),
        "final_url": final_url,
        "recovered_via": recovered_via,
        "language": language,
        "score": original.get("score"),
        "title": original.get("title") or original.get("filename_suggestion"),
        "score_breakdown": original.get("score_breakdown"),
        "reasoning": original.get("llm_reasoning") or original.get("reasoning"),
        "filename_suggestion": original.get("filename_suggestion"),
        "city": city,
        "original_error": original.get("error"),
        "original_error_type": original.get("error_type"),
        "original_failure_timestamp": original.get("timestamp"),
        "recovery_timestamp": _now_iso(),
    }
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return dest


WORKERS = 10


def _process_one(rec: dict, idx: int, total: int) -> dict:
    """Process a single record. Returns a result dict for the caller to merge."""
    url = rec["url"]
    city = rec["city"]
    score = rec.get("score") or 0
    iso3 = city["country_code_iso3"]

    host = urlparse(url).netloc.lower()
    prefix = f"[{idx}/{total}] {city['name']}, {iso3} (score {score})"

    if host in HARD_SKIP_HOSTS:
        print(f"{prefix}\n  ⊘ Hard-skip: {host}")
        return {"url": url, "status": "skipped_hard", "reason": host}

    filename = _filename_from_url(
        url, rec.get("filename_suggestion") or f"plan_{city['loc_id']}.pdf"
    )
    exists, where = already_downloaded(city, url, filename)
    if exists:
        print(f"{prefix}\n  ⊘ Already in {where}")
        return {"url": url, "status": "skipped_existing", "where": where}

    print(f"{prefix}\n  URL: {url[:100]}")
    try:
        pdf_bytes, via, err = recover_url(url)
    except Exception as e:  # noqa: BLE001
        pdf_bytes, via, err = None, "", str(e)

    if pdf_bytes and _is_pdf_bytes(pdf_bytes):
        dest = _save_pdf(pdf_bytes, city, filename, rec, via, url)
        print(f"  ✓ {city['name']} recovered via {via} → {dest.relative_to(BASE_PATH)}")
        return {"url": url, "status": "recovered", "via": via, "path": str(dest.relative_to(BASE_PATH))}
    elif pdf_bytes:
        print(f"  ✗ {city['name']} bytes not %PDF (via={via})")
        return {"url": url, "status": "still_failed", "via": via, "error": f"bytes not %PDF (via {via})", "rec": rec}
    else:
        print(f"  ✗ {city['name']} all strategies failed: {err}")
        return {"url": url, "status": "still_failed", "error": err[:200], "rec": rec}


def main() -> None:
    if not FAILED_FILE.exists():
        print(f"Error: {FAILED_FILE} does not exist")
        sys.exit(1)

    failed = json.loads(FAILED_FILE.read_text())
    progress = load_progress()
    progress["total"] = len(failed)

    failed_sorted = sorted(
        failed,
        key=lambda r: (-(r.get("score") or 0), r.get("error_type", "")),
    )

    seen_urls: set[str] = set()
    unique_failed: list[dict] = []
    for rec in failed_sorted:
        url = rec.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_failed.append(rec)

    # Filter to only unprocessed
    pending = [
        (i, rec) for i, rec in enumerate(unique_failed, 1)
        if rec["url"] not in progress["urls_processed"]
    ]

    print(f"Loaded {len(failed)} records ({len(unique_failed)} unique, {len(pending)} pending)")
    print(
        f"State so far — recovered: {progress['recovered']}, "
        f"skipped(existing): {progress['skipped_existing']}, "
        f"skipped(hard): {progress['skipped_hard']}, "
        f"still failed: {progress['still_failed']}"
    )
    print(f"Output: {INCOMING_DIR.relative_to(BASE_PATH)}/  [{WORKERS} workers]")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)

    still_failed: list[dict] = []
    if STILL_FAILED_FILE.exists():
        try:
            still_failed = json.loads(STILL_FAILED_FILE.read_text())
        except Exception:
            still_failed = []

    lock = threading.Lock()
    total = len(unique_failed)
    shutdown = threading.Event()

    def flush_result(result: dict) -> None:
        url = result["url"]
        status = result["status"]
        entry: dict = {"status": status, "ts": _now_iso()}

        if status == "skipped_hard":
            entry["reason"] = result.get("reason", "")
            progress["skipped_hard"] += 1
        elif status == "skipped_existing":
            entry["where"] = result.get("where", "")
            progress["skipped_existing"] += 1
        elif status == "recovered":
            entry["via"] = result.get("via", "")
            entry["path"] = result.get("path", "")
            progress["recovered"] += 1
        else:  # still_failed
            entry["error"] = result.get("error", "")[:200]
            if "via" in result:
                entry["via"] = result["via"]
            progress["still_failed"] += 1
            rec = result.get("rec")
            if rec:
                still_failed.append({**rec, "recovery_error": result.get("error", "")})

        progress["urls_processed"][url] = entry
        save_progress(progress)
        save_still_failed(still_failed)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(_process_one, rec, idx, total): rec
                for idx, rec in pending
            }
            for future in as_completed(futures):
                if shutdown.is_set():
                    future.cancel()
                    continue
                try:
                    result = future.result()
                except Exception as e:  # noqa: BLE001
                    rec = futures[future]
                    result = {"url": rec["url"], "status": "still_failed", "error": str(e), "rec": rec}
                with lock:
                    flush_result(result)

    except KeyboardInterrupt:
        shutdown.set()
        print("\n⚠️  Interrupted. Progress saved; rerun to resume.")
        sys.exit(0)

    print("\n" + "=" * 60)
    print("RECOVERY COMPLETE")
    print(f"  Total URLs:         {len(unique_failed)}")
    print(f"  Recovered:          {progress['recovered']}")
    print(f"  Skipped (existing): {progress['skipped_existing']}")
    print(f"  Skipped (hard):     {progress['skipped_hard']}")
    print(f"  Still failed:       {progress['still_failed']}")
    print(f"  Output dir:         {INCOMING_DIR.relative_to(BASE_PATH)}")
    print(f"  Still-failed log:   {STILL_FAILED_FILE.relative_to(BASE_PATH)}")
    print("=" * 60)


if __name__ == "__main__":
    # Suppress noisy SSL warnings when verify=False is used.
    try:
        import urllib3  # type: ignore[import-not-found]

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    # Also suppress httpx's underlying warning.
    ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
    main()
