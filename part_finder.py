"""
part_finder.py — Automated datasheet and KiCad symbol downloader.

For each part number this downloads:
  - PDF datasheets  (Mouser Search API → DataSheetUrl, falling back to
                     DuckDuckGo search → direct PDF, then alldatasheet)
  - KiCad symbols   (local cache of the official KiCad symbol libraries,
                     downloaded once and indexed by symbol name)
"""

import asyncio
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 20.0

_MOUSER_SEARCH_URL = "https://api.mouser.com/api/v1/search/partnumber"
# Mouser's free API tier allows ~30 calls per minute
_MOUSER_MIN_INTERVAL = 2.1
_mouser_lock = asyncio.Lock()
_mouser_last_call = 0.0

CACHE_DIR = Path("cache")
_KICAD_DIR = CACHE_DIR / "kicad-symbols"
_KICAD_LIB_DIR = _KICAD_DIR / "lib"
_KICAD_INDEX_PATH = _KICAD_DIR / "index.json"
_KICAD_ARCHIVE_URLS = [
    "https://gitlab.com/kicad/libraries/kicad-symbols/-/archive/master/kicad-symbols-master.zip",
    "https://github.com/KiCad/kicad-symbols/archive/refs/heads/master.zip",
]
_kicad_index: dict[str, dict] | None = None
_kicad_index_lock = asyncio.Lock()

# Top-level symbol names; unit sub-symbols are named "<Parent>_<unit>_<style>"
_SYMBOL_NAME_RE = re.compile(r'^\s*\(symbol\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)
_UNIT_NAME_RE = re.compile(r"_\d+_\d+$")


@dataclass
class DatasheetResult:
    pdf: bytes | None
    status: str                 # "found" | "not found"
    source: str = ""            # "mouser" | "web" | ""
    product_url: str = ""       # Mouser product page, when the part was found there


@dataclass
class SymbolResult:
    text: str | None
    status: str                 # "found" | "not found" | "library unavailable"
    match: str = ""             # KiCad symbol name that was matched


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_parts_list(raw: str) -> list[str]:
    """Split a comma- or newline-separated part list into cleaned strings."""
    return [p.strip() for p in re.split(r"[,\n\r]+", raw) if p.strip()]


def safe_filename(part: str) -> str:
    """Turn a part number into a filesystem-safe file stem."""
    name = re.sub(r'[<>:"/\\|?*#%&{}$!\'@+`=\x00-\x1f]', "_", part.strip())
    name = name.strip(" .")
    return name or "part"


async def find_datasheet(part: str) -> DatasheetResult:
    """Search for and download a datasheet PDF, trying Mouser first."""
    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=_TIMEOUT
    ) as client:
        product_url = ""

        # 1. Mouser Search API
        if os.getenv("MOUSER_API_KEY"):
            ds_url, product_url = await _mouser_lookup(client, part)
            if ds_url:
                pdf = await _try_download_pdf(
                    client, ds_url, referer=product_url or "https://www.mouser.com/"
                )
                if pdf:
                    return DatasheetResult(pdf, "found", "mouser", product_url)

        # 2. DuckDuckGo HTML search for filetype:pdf
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
                        return DatasheetResult(result, "found", "web", product_url)
        except Exception:
            pass

        # 3. Fallback: alldatasheet.com search page
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
                        return DatasheetResult(result, "found", "web", product_url)
        except Exception:
            pass

    return DatasheetResult(None, "not found", "", product_url)


async def find_kicad_symbol(part: str) -> SymbolResult:
    """
    Look up a part in the official KiCad symbol libraries.
    Returns a standalone .kicad_sym file containing just the matched symbol,
    with derived symbols (extends) flattened so the pins are included.
    """
    index = await ensure_kicad_index()
    if not index:
        return SymbolResult(None, "library unavailable")

    entry = _match_symbol(index, part)
    if not entry:
        return SymbolResult(None, "not found")

    try:
        text = await asyncio.to_thread(_build_symbol_file, index, entry)
    except Exception:
        text = None
    if not text:
        return SymbolResult(None, "not found")
    return SymbolResult(text, "found", entry["name"])


async def ensure_kicad_index() -> dict[str, dict] | None:
    """Download and index the KiCad symbol libraries on first use."""
    global _kicad_index
    if _kicad_index is not None:
        return _kicad_index

    async with _kicad_index_lock:
        if _kicad_index is not None:
            return _kicad_index

        if _KICAD_INDEX_PATH.exists():
            try:
                _kicad_index = json.loads(_KICAD_INDEX_PATH.read_text(encoding="utf-8"))
                return _kicad_index
            except (json.JSONDecodeError, OSError):
                pass

        if not await _download_kicad_libraries():
            return None

        _kicad_index = await asyncio.to_thread(_build_kicad_index)
        return _kicad_index


# ---------------------------------------------------------------------------
# Mouser helpers
# ---------------------------------------------------------------------------

async def _mouser_throttle() -> None:
    global _mouser_last_call
    async with _mouser_lock:
        wait = _mouser_last_call + _MOUSER_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _mouser_last_call = time.monotonic()


async def _mouser_lookup(client: httpx.AsyncClient, part: str) -> tuple[str, str]:
    """Return (datasheet_url, product_detail_url) from Mouser, or empty strings."""
    await _mouser_throttle()
    try:
        r = await client.post(
            _MOUSER_SEARCH_URL,
            params={"apiKey": os.getenv("MOUSER_API_KEY", "")},
            json={"SearchByPartRequest": {"mouserPartNumber": part, "partSearchOptions": "None"}},
        )
        if r.status_code != 200:
            return "", ""
        data = r.json()
    except Exception:
        return "", ""

    if data.get("Errors"):
        log.warning("Mouser API error for %s: %s", part,
                    "; ".join(e.get("Message", "") for e in data["Errors"]))
    parts = (data.get("SearchResults") or {}).get("Parts") or []
    if not parts:
        return "", ""

    wanted = part.strip().upper()
    exact = [p for p in parts if (p.get("ManufacturerPartNumber") or "").upper() == wanted]
    ordered = exact + [p for p in parts if p not in exact]

    product_url = (ordered[0].get("ProductDetailUrl") or "")
    for p in ordered:
        ds = (p.get("DataSheetUrl") or "").strip()
        if ds:
            return ds, p.get("ProductDetailUrl") or product_url
    return "", product_url


async def _try_download_pdf(
    client: httpx.AsyncClient, url: str, referer: str | None = None
) -> bytes | None:
    """Download url and return bytes if it looks like a valid PDF, else None."""
    try:
        headers = {"Referer": referer} if referer else None
        r = await client.get(url, timeout=15, headers=headers)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and (
            "pdf" in ct.lower() or r.content[:4] == b"%PDF"
        ):
            return r.content
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# KiCad library cache
# ---------------------------------------------------------------------------

async def _download_kicad_libraries() -> bool:
    """Download the KiCad symbol library archive and extract its .kicad_sym files."""
    _KICAD_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _KICAD_DIR / "kicad-symbols.zip"

    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)
    ) as client:
        for url in _KICAD_ARCHIVE_URLS:
            try:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        continue
                    with zip_path.open("wb") as f:
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
                await asyncio.to_thread(_extract_kicad_archive, zip_path)
                return True
            except Exception:
                continue
            finally:
                if zip_path.exists():
                    zip_path.unlink()
    return False


def _extract_kicad_archive(zip_path: Path) -> None:
    _KICAD_LIB_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir() or not member.filename.lower().endswith(".kicad_sym"):
                continue
            # Drop the archive's top-level "kicad-symbols-master/" folder
            rel = Path(*Path(member.filename).parts[1:])
            dest = (_KICAD_LIB_DIR / rel).resolve()
            if _KICAD_LIB_DIR.resolve() not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))


def _build_kicad_index() -> dict[str, dict]:
    """Map lowercase symbol name -> {name, file} across every library file."""
    index: dict[str, dict] = {}
    for path in sorted(_KICAD_LIB_DIR.rglob("*.kicad_sym")):
        rel = path.relative_to(_KICAD_LIB_DIR).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SYMBOL_NAME_RE.finditer(text):
            name = m.group(1)
            if _UNIT_NAME_RE.search(name):
                continue
            index.setdefault(name.lower(), {"name": name, "file": rel})
    _KICAD_INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    return index


def _candidate_names(part: str) -> list[str]:
    """Part number variants to try, most specific first (e.g. FT231XS-R -> FT231XS)."""
    base = part.strip()
    stems = [base]
    if "/" in base:
        stems.append(base.replace("/", "-"))
    s = base
    while True:
        cut = max(s.rfind("-"), s.rfind("/"), s.rfind("#"))
        if cut <= 2:
            break
        s = s[:cut]
        stems.append(s)

    out: list[str] = []
    for stem in stems:
        out.append(stem)
        t = stem
        while len(t) > 4 and t[-1].isalpha() and re.search(r"\d", t[:-1]):
            t = t[:-1]
            out.append(t)

    unique: list[str] = []
    for c in out:
        if c.lower() not in (u.lower() for u in unique):
            unique.append(c)
    return unique


def _match_symbol(index: dict[str, dict], part: str) -> dict | None:
    for cand in _candidate_names(part):
        entry = index.get(cand.lower())
        if entry:
            return entry

    # Library names that extend the part number, e.g. NE555 -> NE555P
    key = part.strip().lower()
    if len(key) >= 4:
        prefixed = [k for k in index if k.startswith(key)]
        if prefixed:
            return index[min(prefixed, key=len)]
    return None


def _build_symbol_file(index: dict[str, dict], entry: dict) -> str | None:
    lib_text = (_KICAD_LIB_DIR / entry["file"]).read_text(encoding="utf-8", errors="replace")
    block = _resolve_symbol(index, entry["name"], lib_text, depth=0)
    if not block:
        return None

    ver_m = re.search(r"\(version\s+(\d+)\)", lib_text)
    version = ver_m.group(1) if ver_m else "20231120"
    return (
        f"(kicad_symbol_lib\n"
        f"\t(version {version})\n"
        f"\t(generator kicad_symbol_editor)\n"
        f"\t{block}\n"
        f")\n"
    )


def _resolve_symbol(index: dict[str, dict], name: str, lib_text: str, depth: int) -> str | None:
    """Extract a symbol block, flattening any (extends "Parent") chain."""
    block = _extract_block(lib_text, name)
    if not block or depth > 5:
        return block

    ext = re.search(r'\(extends\s+"((?:[^"\\]|\\.)*)"\s*\)', block)
    if not ext:
        return block

    parent_name = ext.group(1)
    parent_text = lib_text
    if not _extract_block(lib_text, parent_name):
        parent_entry = index.get(parent_name.lower())
        if not parent_entry:
            return None
        parent_text = (_KICAD_LIB_DIR / parent_entry["file"]).read_text(
            encoding="utf-8", errors="replace"
        )
    parent_block = _resolve_symbol(index, parent_name, parent_text, depth + 1)
    if not parent_block:
        return None
    return _flatten(block, name, parent_block, parent_name)


def _flatten(child_block: str, child_name: str, parent_block: str, parent_name: str) -> str:
    """Copy the parent's graphics and pins under the child's name and properties."""
    body = re.sub(
        r'\(symbol\s+"' + re.escape(parent_name) + r'"',
        lambda _: f'(symbol "{child_name}"', parent_block, count=1,
    )
    body = re.sub(
        r'\(symbol\s+"' + re.escape(parent_name) + r'_(\d+_\d+)"',
        lambda m: f'(symbol "{child_name}_{m.group(1)}"', body,
    )

    for key, prop in _properties(child_block):
        span = _property_span(body, key)
        if span:
            body = body[:span[0]] + prop + body[span[1]:]
        else:
            unit = re.search(r'\(symbol\s+"', body[1:])
            at = unit.start() + 1 if unit else len(body) - 1
            body = body[:at] + prop + "\n\t\t" + body[at:]
    return body


def _properties(block: str) -> list[tuple[str, str]]:
    props = []
    for m in re.finditer(r'\(property\s+"((?:[^"\\]|\\.)*)"', block):
        end = _balanced_end(block, m.start())
        if end is not None:
            props.append((m.group(1), block[m.start():end + 1]))
    return props


def _property_span(block: str, key: str) -> tuple[int, int] | None:
    m = re.search(r'\(property\s+"' + re.escape(key) + r'"', block)
    if not m:
        return None
    end = _balanced_end(block, m.start())
    return (m.start(), end + 1) if end is not None else None


def _extract_block(text: str, name: str) -> str | None:
    m = re.search(r'\(symbol\s+"' + re.escape(name) + r'"', text)
    if not m:
        return None
    end = _balanced_end(text, m.start())
    return text[m.start():end + 1] if end is not None else None


def _balanced_end(text: str, start: int) -> int | None:
    """Index of the ')' closing the '(' at start, skipping quoted strings."""
    depth = 0
    in_str = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None
