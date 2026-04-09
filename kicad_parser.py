"""
kicad_parser.py — Parse KiCad 6+ symbol files (.kicad_sym) to extract
component pin data for use in circuit design prompts.

KiCad symbol files use S-expression syntax. Each pin has:
  - number: pad/pin number (string)
  - name:   signal name  (e.g. "VCC", "GND", "IN+")
  - type:   electrical type (input, output, bidirectional, passive,
            power_in, power_out, open_collector, open_emitter, no_connect, etc.)

The bounding_box is derived from rectangle nodes in the symbol body, or
estimated from pin count when no rectangle is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MM_TO_PX = 3.78
DEFAULT_SIZE_PX = 80.0
MIN_SIZE_PX = 40.0


@dataclass
class KiCadPin:
    number: str
    name: str
    pin_type: str  # input | output | bidirectional | passive | power_in | ...


@dataclass
class KiCadSymbol:
    name: str
    pins: list[KiCadPin] = field(default_factory=list)
    bounding_box: dict = field(
        default_factory=lambda: {"width": DEFAULT_SIZE_PX, "height": DEFAULT_SIZE_PX}
    )


# ---------------------------------------------------------------------------
# S-expression tokeniser + parser
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\n\r":
            i += 1
        elif c == "(":
            tokens.append("(")
            i += 1
        elif c == ")":
            tokens.append(")")
            i += 1
        elif c == '"':
            # Quoted string — handle backslash escapes
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                elif text[j] == '"':
                    break
                else:
                    j += 1
            tokens.append(text[i + 1 : j])
            i = j + 1
        else:
            # Bare token (keyword, number, identifier)
            j = i
            while j < n and text[j] not in ' \t\n\r()"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _parse_sexp(tokens: list[str], pos: int) -> tuple[object, int]:
    if pos >= len(tokens):
        return None, pos
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        result: list = []
        while pos < len(tokens) and tokens[pos] != ")":
            child, pos = _parse_sexp(tokens, pos)
            if child is not None:
                result.append(child)
        if pos < len(tokens):
            pos += 1  # consume ')'
        return result, pos
    return tok, pos + 1


def _parse(text: str) -> object:
    tokens = _tokenize(text)
    if not tokens:
        return []
    node, _ = _parse_sexp(tokens, 0)
    return node


# ---------------------------------------------------------------------------
# Symbol extraction helpers
# ---------------------------------------------------------------------------

def _find_child(node: list, key: str) -> list | None:
    for item in node:
        if isinstance(item, list) and item and item[0] == key:
            return item
    return None


def _extract_pins_recursive(node: list) -> list[KiCadPin]:
    """Walk the S-expression tree and collect all pin definitions."""
    pins: list[KiCadPin] = []
    if not isinstance(node, list):
        return pins
    if node and node[0] == "pin" and len(node) >= 3:
        pin_type = node[1]
        pin_name = ""
        pin_number = ""
        for child in node[2:]:
            if isinstance(child, list) and len(child) >= 2:
                if child[0] == "name":
                    pin_name = child[1]
                elif child[0] == "number":
                    pin_number = child[1]
        pins.append(KiCadPin(number=pin_number, name=pin_name, pin_type=pin_type))
    else:
        for child in node:
            if isinstance(child, list):
                pins.extend(_extract_pins_recursive(child))
    return pins


def _collect_rectangles(node: list, result: list) -> None:
    """Recursively find rectangle nodes and append (x1, y1, x2, y2) tuples in mm."""
    if not isinstance(node, list):
        return
    if node and node[0] == "rectangle":
        start = _find_child(node, "start")
        end = _find_child(node, "end")
        if start and end and len(start) >= 3 and len(end) >= 3:
            try:
                result.append(
                    (float(start[1]), float(start[2]), float(end[1]), float(end[2]))
                )
            except (ValueError, IndexError):
                pass
    for child in node:
        if isinstance(child, list):
            _collect_rectangles(child, result)


def _extract_bbox(sym_node: list, pin_count: int) -> dict:
    """
    Derive bounding_box in SVG px.
    Prefer the largest rectangle in the symbol body; fall back to pin-count estimate.
    """
    rects: list[tuple[float, float, float, float]] = []
    _collect_rectangles(sym_node, rects)

    if rects:
        best = max(rects, key=lambda r: abs(r[2] - r[0]) * abs(r[3] - r[1]))
        x1, y1, x2, y2 = best
        w = max(abs(x2 - x1) * MM_TO_PX, MIN_SIZE_PX)
        h = max(abs(y2 - y1) * MM_TO_PX, MIN_SIZE_PX)
        return {"width": round(w, 1), "height": round(h, 1)}

    # Estimate: allow ~12 px per pin, distribute half per side
    size = max(DEFAULT_SIZE_PX, pin_count * 12.0)
    return {"width": round(size, 1), "height": round(size, 1)}


def _pin_sort_key(number: str) -> tuple:
    """Sort pin numbers naturally: '1', '2', '10' before 'A1', 'B2'."""
    try:
        return (0, int(number), "")
    except ValueError:
        return (1, 0, number)


def _build_symbol(name: str, sym_node: list) -> KiCadSymbol:
    pins = _extract_pins_recursive(sym_node)
    # De-duplicate by pin number (sub-symbol units repeat definitions)
    seen: set[str] = set()
    unique: list[KiCadPin] = []
    for p in pins:
        if p.number not in seen:
            seen.add(p.number)
            unique.append(p)
    unique.sort(key=lambda p: _pin_sort_key(p.number))
    bbox = _extract_bbox(sym_node, len(unique))
    return KiCadSymbol(name=name, pins=unique, bounding_box=bbox)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_kicad_sym(file_path: Path) -> list[KiCadSymbol]:
    """
    Parse a .kicad_sym file and return all KiCadSymbol objects it contains.

    A file downloaded from SnapEDA/Ultra Librarian typically contains one symbol.
    KiCad's bundled libraries may contain many symbols per file.
    """
    text = file_path.read_text(encoding="utf-8", errors="replace")
    tree = _parse(text)

    if not isinstance(tree, list) or not tree:
        return []

    # tree[0] == 'kicad_symbol_lib'
    # Direct children with tag 'symbol' are top-level components.
    # Sub-symbols ("Name_0_1", "Name_1_1") are nested inside them — not here.
    symbols: list[KiCadSymbol] = []
    for child in tree[1:]:
        if isinstance(child, list) and child and child[0] == "symbol":
            sym_name = child[1] if len(child) > 1 else file_path.stem
            symbols.append(_build_symbol(sym_name, child))

    return symbols


def get_kicad_symbols_for_part(project: object, part_stem: str) -> list[KiCadSymbol]:
    """
    Load the .kicad_sym file matching part_stem from the project's symbols_dir.
    Returns a list of KiCadSymbol (usually just one per file).
    """
    if not project.symbols_dir.exists():
        return []

    stem_lower = part_stem.lower()
    for p in project.symbols_dir.iterdir():
        if p.suffix.lower() == ".kicad_sym" and p.stem.lower() == stem_lower:
            try:
                return parse_kicad_sym(p)
            except Exception:
                return []
    return []
