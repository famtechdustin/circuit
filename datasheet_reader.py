"""
datasheet_reader.py — PDF text extraction for component datasheets.

Datasheets are placed by the user in projects/<name>/datasheets/<PartName>.pdf.
Matching is case-insensitive on the filename stem.
"""

from pathlib import Path

import pymupdf  # PyMuPDF; also importable as fitz

MAX_CHARS_PER_DATASHEET = 40_000  # ~10k tokens; keeps datasheet context manageable


def extract_text(pdf_path: Path) -> str:
    """
    Extract all text from a PDF and return up to MAX_CHARS_PER_DATASHEET characters.
    Raises FileNotFoundError if the path does not exist.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"Datasheet not found: {pdf_path}")

    doc = pymupdf.open(str(pdf_path))
    parts: list[str] = []
    total = 0
    for page in doc:
        text = page.get_text("text")
        remaining = MAX_CHARS_PER_DATASHEET - total
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        total += len(text)
        if total >= MAX_CHARS_PER_DATASHEET:
            break
    doc.close()
    return "\n".join(parts)


def extract_text_for_parts(
    project: "object",  # Project dataclass from project_manager
    part_names: list[str],
) -> dict[str, str]:
    """
    For each part name, look for <name>.pdf (case-insensitive) in datasheets_dir.
    Returns {part_name: extracted_text} for parts whose datasheet exists.
    Parts with no matching PDF are omitted.
    """
    result: dict[str, str] = {}
    if not project.datasheets_dir.exists():
        return result

    # Build a lowercase stem → actual path map
    available: dict[str, Path] = {
        p.stem.lower(): p
        for p in project.datasheets_dir.iterdir()
        if p.suffix.lower() == ".pdf"
    }

    for name in part_names:
        path = available.get(name.lower())
        if path is not None:
            try:
                result[name] = extract_text(path)
            except Exception:
                pass  # skip unreadable PDFs silently

    return result


def find_missing_datasheets(
    project: "object",
    part_names: list[str],
) -> list[str]:
    """
    Return the subset of part_names that have no matching PDF in datasheets_dir.
    Matching is case-insensitive on the filename stem.
    """
    if not project.datasheets_dir.exists():
        return list(part_names)

    available_stems = {
        p.stem.lower()
        for p in project.datasheets_dir.iterdir()
        if p.suffix.lower() == ".pdf"
    }
    return [name for name in part_names if name.lower() not in available_stems]
