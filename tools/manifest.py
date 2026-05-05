"""
Shared helpers for adaptbase-plans tools.

Provides:
- sha256_file(): Compute SHA256 hash of a file
- extract_title(): Extract title from PDF metadata
- chunk_text(): Chunk text with token counting and overlap
- get_supabase_client(): Create Supabase client with service role key
"""

import csv
import hashlib
import os
from pathlib import Path
from typing import NamedTuple

import pymupdf
import tiktoken
from dotenv import load_dotenv
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "reference"

# Load environment variables
load_dotenv()


class Chunk(NamedTuple):
    """Text chunk with metadata."""

    text: str
    chunk_index: int
    page_number: int


def sha256_file(file_path: str | Path) -> str:
    """
    Compute SHA256 hash of a file.

    Args:
        file_path: Path to file

    Returns:
        Hexadecimal SHA256 hash
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in 64kb chunks to handle large files
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def extract_title(pdf_path: str | Path) -> str:
    """
    Extract title from PDF metadata, falling back to filename.

    Args:
        pdf_path: Path to PDF file

    Returns:
        Document title
    """
    try:
        doc = pymupdf.open(pdf_path)
        metadata = doc.metadata

        # Try various metadata fields
        if metadata and metadata.get("title"):
            title = metadata["title"].strip()
            if title and title.lower() != "untitled":
                return title

        # Fallback to filename
        return Path(pdf_path).stem.replace("_", " ").replace("-", " ")
    except Exception:
        # If PDF can't be opened, use filename
        return Path(pdf_path).stem.replace("_", " ").replace("-", " ")


def chunk_text(
    pages: list[tuple[int, str]],
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[Chunk]:
    """
    Chunk text from PDF pages with token counting and overlap.

    Args:
        pages: List of (page_number, text) tuples
        max_tokens: Maximum tokens per chunk
        overlap_tokens: Number of tokens to overlap between chunks

    Returns:
        List of Chunk objects with text, chunk_index, and page_number
    """
    # Initialize tokenizer (cl100k_base used by text-embedding-3-small)
    encoding = tiktoken.get_encoding("cl100k_base")

    chunks: list[Chunk] = []
    chunk_index = 0

    for page_num, page_text in pages:
        if not page_text.strip():
            continue

        # Tokenize the page
        tokens = encoding.encode(page_text)

        # If page fits in one chunk, add it directly
        if len(tokens) <= max_tokens:
            chunks.append(
                Chunk(
                    text=page_text.strip(),
                    chunk_index=chunk_index,
                    page_number=page_num,
                )
            )
            chunk_index += 1
            continue

        # Otherwise, split into overlapping chunks
        start = 0
        while start < len(tokens):
            end = start + max_tokens
            chunk_tokens = tokens[start:end]

            # Decode back to text
            chunk_text = encoding.decode(chunk_tokens).strip()

            chunks.append(
                Chunk(
                    text=chunk_text,
                    chunk_index=chunk_index,
                    page_number=page_num,
                )
            )
            chunk_index += 1

            # Move start forward, accounting for overlap
            start = end - overlap_tokens

            # Avoid infinite loop if overlap >= max_tokens
            if start <= end - max_tokens:
                start = end

    return chunks


def get_supabase_client():
    """
    Create Supabase client with service role key.

    Returns:
        Supabase client instance

    Raises:
        ValueError: If required environment variables are missing
    """
    url = os.getenv("PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        msg = "Missing PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env"
        raise ValueError(msg)

    return create_client(url, key)


def load_countries() -> dict[str, dict[str, str]]:
    """
    Load country reference data.

    Returns:
        dict keyed by ISO3 code → {"name": str, "iso2": str}
    """
    countries: dict[str, dict[str, str]] = {}
    path = REFERENCE_DIR / "countries.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso3 = (row.get("code_iso3") or "").strip()
            if iso3:
                countries[iso3] = {
                    "name": (row.get("name") or "").strip(),
                    "iso2": (row.get("code_iso2") or "").strip(),
                }
    return countries


def load_cities() -> dict[str, dict[str, str]]:
    """
    Load city reference data.

    Returns:
        dict keyed by loc_id (str) → {"name": str, "country_iso3": str}
    """
    cities: dict[str, dict[str, str]] = {}
    path = REFERENCE_DIR / "cities.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            loc_id = (row.get("loc_id") or "").strip()
            if loc_id:
                cities[loc_id] = {
                    "name": (row.get("name") or "").strip(),
                    "country_iso3": (row.get("country_code_iso3") or "").strip(),
                }
    return cities


def build_location_metadata(
    relative_path: Path,
    countries: dict[str, dict[str, str]],
    cities: dict[str, dict[str, str]],
) -> dict[str, str]:
    """
    Given a path of the form `{ISO3}/{loc_id}/...filename.pdf`, return a
    metadata dict with country and city info. Unknown codes are left blank
    (logged by caller if desired) so PDFs in _incoming/ etc. still migrate.
    """
    parts = relative_path.parts
    metadata: dict[str, str] = {}

    if len(parts) >= 1:
        iso3 = parts[0]
        country = countries.get(iso3)
        if country:
            metadata["country_iso3"] = iso3
            metadata["country_iso2"] = country["iso2"]
            metadata["country_name"] = country["name"]

    if len(parts) >= 2:
        loc_id = parts[1]
        city = cities.get(loc_id)
        if city:
            metadata["city_loc_id"] = loc_id
            metadata["city_name"] = city["name"]

    return metadata


def format_file_size(size_bytes: int) -> str:
    """
    Format file size in human-readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted size string (e.g., "1.5 MB")
    """
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
