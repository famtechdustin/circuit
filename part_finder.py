"""
part_finder.py — Automated datasheet and KiCad symbol downloader.

Searches public sources for each part number and downloads:
  - PDF datasheets  (DuckDuckGo search → direct PDF, with alldatasheet fallback)
  - KiCad symbols   (GitHub search in KiCad/kicad-symbols → symbol extraction)
"""

import asyncio
import re

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_parts_list(raw: str) -> list[str]:
    """Split a comma- or newline-separated part list into cleaned strings."""
    return [p.strip() for p in re.split(r"[,\n\r]+", raw) if p.strip()]


async def find_datasheet(part: str) -> tuple[bytes | None, str]:
    """
    Search for and download a datasheet PDF for the given part number.
    Returns (pdf_bytes_or_None, status_string).
    """
    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=_TIMEOUT
    ) as client:
        # 1. DuckDuckGo HTML search for filetype:pdf
        try:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f"{part} datasheet filetype:pdf"},
            )
            if r.status_code == 200:
                pdf_links = re.findall(
                    r'href="(https?://[^\s"]+\.pdf[^\s"]*)"',
                    r.text, re.IGNORECASE,
                )
                for link in pdf_links[:6]:
                    result = await _try_download_pdf(client, link)
                    if result:
                        return result, "found"
        except Exception:
            pass

        # 2. Fallback: alldatasheet.com search page
        try:
            r = await client.get(
                "https://www.alldatasheet.com/view.jsp",
                params={"Searchword": part, "sField": "2"},
            )
            if r.status_code == 200:
                matches = re.findall(
                    r'href=["\']([^"\']*\.pdf[^"\']*)["\']',
                    r.text, re.IGNORECASE,
                )
                for m in matches[:4]:
                    url = m if m.startswith("http") else "https://www.alldatasheet.com" + m
                    result = await _try_download_pdf(client, url)
                    if result:
                        return result, "found"
        except Exception:
            pass

    return None, "not found"


async def find_kicad_symbol(part: str) -> tuple[str | None, str]:
    """
    Search KiCad official symbol libraries on GitHub for the given part.
    Returns (kicad_sym_file_content_or_None, status_string).
    Only the matching symbol is extracted from the library file.
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": "circuit-design-app", "Accept": "application/vnd.github.v3+json"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    ) as client:
        try:
            r = await client.get(
                "https://api.github.com/search/code",
                params={"q": f'"{part}" repo:KiCad/kicad-symbols extension:kicad_sym'},
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                for item in items[:3]:
                    raw_url = (
                        item["html_url"]
                        .replace("github.com", "raw.githubusercontent.com")
                        .replace("/blob/", "/")
                    )
                    raw_r = await client.get(raw_url)
                    if raw_r.status_code == 200:
                        extracted = _extract_symbol(raw_r.text, part)
                        if extracted:
                            return extracted, "found"
        except Exception:
            pass

    return None, "not found"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _try_download_pdf(
    client: httpx.AsyncClient, url: str
) -> bytes | None:
    """Download url and return bytes if it looks like a valid PDF, else None."""
    try:
        r = await client.get(url, timeout=15)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and (
            "pdf" in ct.lower() or r.content[:4] == b"%PDF"
        ):
            return r.content
    except Exception:
        pass
    return None


def _extract_symbol(library_text: str, part: str) -> str | None:
    """
    Extract a single symbol block from a KiCad .kicad_sym library file.
    Returns a minimal but valid standalone .kicad_sym file, or None.
    """
    # Exact match first, then partial
    for pattern_str in [
        r'\(symbol\s+"' + re.escape(part) + r'"',
        r'\(symbol\s+"[^"]*' + re.escape(part) + r'[^"]*"',
    ]:
        m = re.search(pattern_str, library_text, re.IGNORECASE)
        if m:
            break
    else:
        return None

    # Walk forward to find the balanced closing paren
    start = m.start()
    depth = 0
    end = start
    for i, ch in enumerate(library_text[start:], start=start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return None

    sym_block = library_text[start:end + 1]

    # Reuse the library header (version + generator line)
    hdr_m = re.search(r"\(kicad_symbol_lib\s+\(version\s+\d+\)", library_text)
    header = hdr_m.group(0) if hdr_m else "(kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor)"
    if not header.endswith(")"):
        header += ")"
    # Re-open the header (strip last paren so we can inject the symbol)
    header_open = header[:-1]

    return f"{header_open}\n  {sym_block}\n)\n"
