"""
bom_importer.py — Parse DipTrace BOM CSV exports into a de-duplicated part list.

DipTrace BOM columns are user-configurable, so header names are matched against
a list of known variants. Rows are merged by part number so each part is only
fetched once, while RefDes lists and quantities are preserved.
"""

import csv
import io
import re
from dataclasses import dataclass, field

_MPN_HEADERS = [
    "manufacturer part number", "mfr part number", "mfr. part number",
    "mfr part #", "mfr part", "mpn", "part number", "part no", "part #",
    "partnumber", "manufacturer pn", "mfg part number",
]
_NAME_HEADERS = ["name", "component", "part", "part name"]
_VALUE_HEADERS = ["value"]
_REFDES_HEADERS = ["refdes", "ref des", "reference", "references", "designator", "designators", "ref"]
_QTY_HEADERS = ["quantity", "qty", "count"]
_MFR_HEADERS = ["manufacturer", "mfr", "mfg", "manufacturer name"]
_PATTERN_HEADERS = ["pattern", "footprint", "package"]

_ALL_HEADERS = set(
    _MPN_HEADERS + _NAME_HEADERS + _VALUE_HEADERS + _REFDES_HEADERS
    + _QTY_HEADERS + _MFR_HEADERS + _PATTERN_HEADERS
)

# Generic component names DipTrace uses for passives / unnamed parts
_GENERIC_NAMES = {
    "r", "c", "l", "d", "res", "cap", "ind", "resistor", "capacitor",
    "inductor", "led", "diode", "fuse", "jumper", "testpoint", "test point",
    "tp", "header", "conn", "connector", "crystal", "xtal", "ferrite",
    "fb", "pad", "hole", "mounting hole", "fiducial", "net tie",
}

# Bare component values like 10k, 4.7uF, 100nF, 220R, 1M, 0R, 10%, 0.1
_VALUE_RE = re.compile(
    r"^\d+(\.\d+)?\s*([pnuµmkKMGR])?(\d+)?\s*(F|H|Ω|[Oo]hms?)?(\s*[,/ ].*)?$"
)


@dataclass
class BomRow:
    mpn: str
    refdes: list[str] = field(default_factory=list)
    qty: int = 0
    manufacturer: str = ""
    value: str = ""
    pattern: str = ""
    generic: bool = False

    def to_dict(self) -> dict:
        return {
            "mpn": self.mpn,
            "refdes": self.refdes,
            "qty": self.qty,
            "manufacturer": self.manufacturer,
            "value": self.value,
            "pattern": self.pattern,
            "generic": self.generic,
        }


def parse_diptrace_csv(text: str) -> list[BomRow]:
    """Parse a DipTrace BOM CSV export. Raises ValueError if no header row is found."""
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines:
        raise ValueError("BOM file is empty.")

    delimiter = _detect_delimiter(text)
    rows = list(csv.reader(lines, delimiter=delimiter))

    header_idx = _find_header_row(rows)
    if header_idx is None:
        raise ValueError(
            "Could not find a header row. Expected columns like "
            "'RefDes', 'Name', 'Value', 'Quantity', or 'Part Number'."
        )

    header = [h.strip().lower() for h in rows[header_idx]]
    col = {
        "mpn": _find_col(header, _MPN_HEADERS),
        "name": _find_col(header, _NAME_HEADERS),
        "value": _find_col(header, _VALUE_HEADERS),
        "refdes": _find_col(header, _REFDES_HEADERS),
        "qty": _find_col(header, _QTY_HEADERS),
        "mfr": _find_col(header, _MFR_HEADERS),
        "pattern": _find_col(header, _PATTERN_HEADERS),
    }

    merged: dict[str, BomRow] = {}
    for raw in rows[header_idx + 1:]:
        if not any(c.strip() for c in raw):
            continue

        def get(key: str) -> str:
            i = col[key]
            return raw[i].strip() if i is not None and i < len(raw) else ""

        mpn = get("mpn")
        name = get("name")
        value = get("value")
        refdes = [r for r in re.split(r"[,;\s]+", get("refdes")) if r]
        qty = _parse_qty(get("qty"), len(refdes))

        generic = False
        if not mpn:
            if name and not _looks_generic(name):
                mpn = name
            elif value and not _looks_generic(value) and _looks_like_mpn(value):
                mpn = value
            else:
                mpn = name or value or (refdes[0] if refdes else "")
                generic = True
        if not mpn:
            continue

        key = mpn.upper() if not generic else f"{mpn.upper()}|{value.upper()}|{get('pattern').upper()}"
        row = merged.get(key)
        if row is None:
            row = BomRow(
                mpn=mpn,
                manufacturer=get("mfr"),
                value=value,
                pattern=get("pattern"),
                generic=generic,
            )
            merged[key] = row
        row.refdes.extend(r for r in refdes if r not in row.refdes)
        row.qty += qty

    return list(merged.values())


def _detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def _find_header_row(rows: list[list[str]]) -> int | None:
    for i, row in enumerate(rows[:30]):
        cells = {c.strip().lower() for c in row}
        if len(cells & _ALL_HEADERS) >= 2:
            return i
    return None


def _find_col(header: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        if cand in header:
            return header.index(cand)
    return None


def _parse_qty(raw: str, fallback: int) -> int:
    m = re.search(r"\d+", raw)
    if m:
        return int(m.group(0))
    return fallback or 1


def _looks_generic(s: str) -> bool:
    s = s.strip()
    return s.lower() in _GENERIC_NAMES or bool(_VALUE_RE.match(s))


def _looks_like_mpn(s: str) -> bool:
    s = s.strip()
    return len(s) >= 4 and bool(re.search(r"[A-Za-z]", s)) and bool(re.search(r"\d", s))
