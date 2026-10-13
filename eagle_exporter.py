"""
eagle_exporter.py — Export a circuit JSON dict as an EAGLE XML schematic (.sch).

The output is meant for DipTrace Schematic (File → Import → Eagle). Each
component gets its own rectangular symbol built from the JSON pin sides/offsets,
plus a minimal package whose pads are the pin numbers. Connectivity is carried
by <pinref> entries in each <net>; every connected pin gets a short wire stub
and a net label, so no routed wires are needed.

All coordinates are in mm on a 0.1" (2.54 mm) grid so pins land on DipTrace's
default grid.
"""

import copy
import re
import xml.etree.ElementTree as ET

import svg_generator

GRID = 2.54             # mm per grid step (0.1")
PX_PER_GRID = 10        # SVG px per grid step when mapping JSON positions
PIN_LEN = 1             # grid steps ("short" EAGLE pin)
STUB_LEN = 2            # grid steps of wire between pin tip and net label
GATE = "G$1"
LIB = "circuit"

_DIRECTIONS = {
    "input": "in",
    "output": "out",
    "bidirectional": "io",
    "passive": "pas",
    "power_in": "pwr",
    "power_out": "out",
    "open_collector": "oc",
    "no_connect": "nc",
}

_LAYERS = [
    (1, "Top", 4), (16, "Bottom", 1), (17, "Pads", 2), (18, "Vias", 2),
    (20, "Dimension", 15), (21, "tPlace", 7), (25, "tNames", 7),
    (27, "tValues", 7), (51, "tDocu", 7), (91, "Nets", 2), (92, "Busses", 1),
    (94, "Symbols", 4), (95, "Names", 7), (96, "Values", 7), (97, "Info", 7),
]


def _mm(steps: float) -> str:
    return f"{steps * GRID:.4f}".rstrip("0").rstrip(".")


def _ident(text: str, fallback: str, upper: bool = True) -> str:
    """EAGLE names: no whitespace, no quotes; keep it ASCII-safe."""
    s = re.sub(r"[^A-Za-z0-9_\-+./#$]", "_", str(text or "").strip())
    return (s.upper() if upper else s) or fallback


def _spread(offsets: list[float], length: int) -> list[int]:
    """Map 0–1 offsets to distinct grid positions in 1..length-1, keeping order."""
    n = len(offsets)
    pos = [min(max(round(o * length), 1), length - 1) for o in offsets]
    if len(set(pos)) == n:
        return pos
    start = (length - (n - 1)) // 2
    return [start + i for i in range(n)]


def _layout_pins(comp: dict) -> tuple[int, int, list[dict]]:
    """
    Return (width, height, pins) in grid steps. Each pin dict carries its EAGLE
    name, pad, direction, side, and tip coordinates relative to the body's
    top-left corner (y grows downward here; flipped when written).
    """
    pins = comp.get("pins", [])
    by_side: dict[str, list[dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        by_side.get(p.get("side", "left"), by_side["left"]).append(p)
    for side_pins in by_side.values():
        side_pins.sort(key=lambda p: float(p.get("offset", 0.5)))

    box = comp.get("bounding_box", {})
    w = max(round(float(box.get("width", 60)) / PX_PER_GRID),
            len(by_side["top"]) + 1, len(by_side["bottom"]) + 1, 4)
    h = max(round(float(box.get("height", 40)) / PX_PER_GRID),
            len(by_side["left"]) + 1, len(by_side["right"]) + 1, 2)

    names_seen: dict[str, int] = {}
    for p in pins:
        n = _ident(p.get("name") or p.get("number"), "P")
        names_seen[n] = names_seen.get(n, 0) + 1

    pads_used: set[str] = set()
    laid_out = []
    for side, side_pins in by_side.items():
        length = h if side in {"left", "right"} else w
        positions = _spread([float(p.get("offset", 0.5)) for p in side_pins], length)
        for p, at in zip(side_pins, positions):
            number = str(p.get("number", ""))
            name = _ident(p.get("name") or number, "P")
            if names_seen[name] > 1:
                name = f"{name}@{number}"
            pad = _ident(number, "P")
            while pad in pads_used:
                pad += "_"
            pads_used.add(pad)

            if side == "left":
                tip = (-PIN_LEN, at)
            elif side == "right":
                tip = (w + PIN_LEN, at)
            elif side == "top":
                tip = (at, -PIN_LEN)
            else:
                tip = (at, h + PIN_LEN)

            laid_out.append({
                "number": number,
                "name": name,
                "pad": pad,
                "side": side,
                "direction": _DIRECTIONS.get(p.get("direction", "passive"), "pas"),
                "tip": tip,
            })
    return w, h, laid_out


def _sub(parent: ET.Element, tag: str, **attrs) -> ET.Element:
    return ET.SubElement(parent, tag, {k.rstrip("_"): str(v) for k, v in attrs.items()})


def _add_symbol(symbols: ET.Element, name: str, w: int, h: int, pins: list[dict]) -> None:
    sym = _sub(symbols, "symbol", name=name)
    corners = [(0, 0), (w, 0), (w, -h), (0, -h)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        _sub(sym, "wire", x1=_mm(x1), y1=_mm(y1), x2=_mm(x2), y2=_mm(y2),
             width="0.254", layer="94")
    _sub(sym, "text", x="0", y=_mm(0.5), size="1.778", layer="95").text = ">NAME"
    _sub(sym, "text", x="0", y=_mm(-h - 1.2), size="1.778", layer="96").text = ">VALUE"

    rot = {"left": "R0", "right": "R180", "top": "R270", "bottom": "R90"}
    for p in pins:
        tx, ty = p["tip"]
        _sub(sym, "pin", name=p["name"], x=_mm(tx), y=_mm(-ty), length="short",
             direction=p["direction"], rot=rot[p["side"]])


def _add_package(packages: ET.Element, name: str, pins: list[dict]) -> None:
    pkg = _sub(packages, "package", name=name)
    for i, p in enumerate(pins):
        _sub(pkg, "smd", name=p["pad"], x=_mm(i), y="0", dx="1.5", dy="1.5", layer="1")
    _sub(pkg, "text", x="0", y=_mm(1), size="1.27", layer="25").text = ">NAME"
    _sub(pkg, "text", x="0", y=_mm(-1), size="1.27", layer="27").text = ">VALUE"


def _bom_by_refdes(bom_rows: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in bom_rows or []:
        for ref in row.get("refdes", []):
            out[ref.upper()] = row
    return out


def generate_eagle_sch(circuit: dict, bom_rows: list[dict] | None = None) -> str:
    """
    Convert a circuit dict to an EAGLE XML schematic string.

    bom_rows: optional rows from bom_report.json. When a component ID matches a
    BOM RefDes, the BOM pattern is used as the package name and the MPN is
    attached as an attribute, so DipTrace can match it to your pattern library.
    """
    components = copy.deepcopy(circuit.get("components", []))
    svg_generator._resolve_overlaps(components)
    bom = _bom_by_refdes(bom_rows)

    root = ET.Element("eagle", version="7.7.0")
    drawing = _sub(root, "drawing")
    _sub(_sub(drawing, "settings"), "setting", alwaysvectorfont="no")
    _sub(drawing, "grid", distance="0.1", unitdist="inch", unit="inch", style="lines",
         multiple="1", display="no", altdistance="0.01", altunitdist="inch", altunit="inch")
    layers = _sub(drawing, "layers")
    for num, lname, color in _LAYERS:
        _sub(layers, "layer", number=num, name=lname, color=color, fill="1",
             visible="yes", active="yes")

    sch = _sub(drawing, "schematic")
    lib = _sub(_sub(sch, "libraries"), "library", name=LIB)
    packages = _sub(lib, "packages")
    symbols = _sub(lib, "symbols")
    devicesets = _sub(lib, "devicesets")
    _sub(sch, "attributes")
    _sub(sch, "variantdefs")
    _sub(_sub(sch, "classes"), "class", number="0", name="default", width="0", drill="0")
    parts = _sub(sch, "parts")
    sheet = _sub(_sub(sch, "sheets"), "sheet")
    _sub(sheet, "plain")
    instances = _sub(sheet, "instances")
    _sub(sheet, "busses")
    nets_el = _sub(sheet, "nets")

    package_pads: dict[str, tuple[str, ...]] = {}
    # (component_id, pin_number) -> (part, pin name, absolute tip, side)
    pin_index: dict[tuple[str, str], tuple[str, str, tuple[int, int], str]] = {}

    for comp in components:
        cid = str(comp.get("id", "")).strip()
        if not cid:
            continue
        part = _ident(cid, "U")
        w, h, pins = _layout_pins(comp)
        row = bom.get(cid.upper(), {})

        dev_name = f"{_ident(comp.get('name') or comp.get('type'), 'PART')}_{part}"
        _add_symbol(symbols, dev_name, w, h, pins)

        pkg_name = _ident(row.get("pattern") or comp.get("footprint"), f"PKG_{part}", upper=False)
        pads = tuple(p["pad"] for p in pins)
        if package_pads.get(pkg_name, pads) != pads:
            pkg_name = f"{pkg_name}_{part}"
        if pkg_name not in package_pads:
            package_pads[pkg_name] = pads
            _add_package(packages, pkg_name, pins)

        prefix = re.match(r"[A-Za-z]*", part).group() or "U"
        ds = _sub(devicesets, "deviceset", name=dev_name, prefix=prefix, uservalue="yes")
        _sub(ds, "description").text = comp.get("name", "")
        _sub(_sub(ds, "gates"), "gate", name=GATE, symbol=dev_name, x="0", y="0")
        dev = _sub(_sub(ds, "devices"), "device", name="", package=pkg_name)
        connects = _sub(dev, "connects")
        for p in pins:
            _sub(connects, "connect", gate=GATE, pin=p["name"], pad=p["pad"])
        _sub(_sub(dev, "technologies"), "technology", name="")

        part_el = _sub(parts, "part", name=part, library=LIB, deviceset=dev_name,
                       device="", value=comp.get("value") or comp.get("name") or part)
        if row.get("mpn") and not row.get("generic"):
            _sub(part_el, "attribute", name="MPN", value=row["mpn"])
        if row.get("manufacturer"):
            _sub(part_el, "attribute", name="MANUFACTURER", value=row["manufacturer"])

        pos = comp.get("position", {})
        ox = round(float(pos.get("x", 0)) / PX_PER_GRID)
        oy = round(float(pos.get("y", 0)) / PX_PER_GRID)
        _sub(instances, "instance", part=part, gate=GATE, x=_mm(ox), y=_mm(-oy))

        for p in pins:
            tx, ty = p["tip"]
            pin_index[(cid, p["number"])] = (part, p["name"], (ox + tx, oy + ty), p["side"])

    stub_dir = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
    merged: dict[str, list[tuple]] = {}
    for i, net in enumerate(circuit.get("nets", []), start=1):
        name = _ident(net.get("name"), f"N${i}")
        for conn in net.get("connections", []):
            key = (str(conn.get("component_id", "")).strip(), str(conn.get("pin_number", "")))
            if key in pin_index:
                merged.setdefault(name, []).append(pin_index[key])

    for name, members in merged.items():
        net_el = _sub(nets_el, "net", name=name, class_="0")
        for part, pin_name, (x, y), side in members:
            dx, dy = stub_dir[side]
            ex, ey = x + dx * STUB_LEN, y + dy * STUB_LEN
            seg = _sub(net_el, "segment")
            _sub(seg, "pinref", part=part, gate=GATE, pin=pin_name)
            _sub(seg, "wire", x1=_mm(x), y1=_mm(-y), x2=_mm(ex), y2=_mm(-ey),
                 width="0.1524", layer="91")
            rot = {"left": "R180", "right": "R0", "top": "R90", "bottom": "R270"}[side]
            _sub(seg, "label", x=_mm(ex), y=_mm(-ey), size="1.778", layer="95", rot=rot)

    ET.indent(root)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )
