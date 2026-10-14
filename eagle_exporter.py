"""
eagle_exporter.py — Export a circuit JSON dict as an EAGLE XML schematic (.sch).

The output is meant for DipTrace Schematic (File → Import → Eagle). Each
component gets its own rectangular symbol built from the JSON pin sides/offsets,
plus a minimal package whose pads are the pin numbers. Connectivity is carried
by continuous Manhattan-routed <wire> elements (plus <junction> on trunks) that
geometrically join every <pinref> in a net, with a single net <label> for
readability. Ground nets instead place a local GND supply symbol at each pin.

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
    common = svg_generator._is_common_part(comp)
    fet = bool(svg_generator._transistor_kind(comp.get("type", "")))
    # Compact defaults for standard glyphs; ICs keep roomier boxes.
    min_w = 3 if common else (5 if fet else 4)
    min_h = 2 if common else (5 if fet else 2)
    w = max(round(float(box.get("width", 60)) / PX_PER_GRID),
            len(by_side["top"]) + 1, len(by_side["bottom"]) + 1, min_w)
    h = max(round(float(box.get("height", 40)) / PX_PER_GRID),
            len(by_side["left"]) + 1, len(by_side["right"]) + 1, min_h)

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


def _sym_wire(sym: ET.Element, x1: float, y1: float, x2: float, y2: float,
              width: str = "0.254") -> None:
    """Symbol graphic wire in grid steps (EAGLE y-up)."""
    _sub(sym, "wire",
         x1=_mm(x1), y1=_mm(y1), x2=_mm(x2), y2=_mm(y2),
         width=width, layer="94")


def _sym_body_rect(sym: ET.Element, w: int, h: int) -> None:
    corners = [(0, 0), (w, 0), (w, -h), (0, -h)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        _sym_wire(sym, x1, y1, x2, y2)


def _pin_axis_y(pins: list[dict], w: int, h: int) -> float:
    """Preferred horizontal-symbol centreline (EAGLE y-up) from L/R pins."""
    ys = [-float(p["tip"][1]) for p in pins if p.get("side") in {"left", "right"}]
    return sum(ys) / len(ys) if ys else -h / 2


def _draw_eagle_resistor(sym: ET.Element, w: int, h: int, pins: list[dict]) -> None:
    """ANSI zigzag between left and right body edges."""
    cy = _pin_axis_y(pins, w, h)
    body = max(w * 0.65, 2.0)
    x0 = (w - body) / 2
    x1 = x0 + body
    amp = min(max(h * 0.32, 0.6), 1.4)
    _sym_wire(sym, 0, cy, x0, cy)
    _sym_wire(sym, x1, cy, w, cy)
    n = 6
    pts = [(x0, cy)]
    for i in range(1, n):
        pts.append((x0 + body * i / n, cy + (amp if i % 2 else -amp)))
    pts.append((x1, cy))
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        _sym_wire(sym, ax, ay, bx, by)


def _draw_eagle_capacitor(sym: ET.Element, w: int, h: int, pins: list[dict]) -> None:
    """Two parallel plates (vertical for left–right pins)."""
    cy = _pin_axis_y(pins, w, h)
    cx = w / 2
    gap = min(max(w * 0.12, 0.35), 0.7)
    hh = min(max(h * 0.35, 0.8), 1.6)
    _sym_wire(sym, 0, cy, cx - gap, cy)
    _sym_wire(sym, cx + gap, cy, w, cy)
    _sym_wire(sym, cx - gap, cy - hh, cx - gap, cy + hh, width="0.4064")
    _sym_wire(sym, cx + gap, cy - hh, cx + gap, cy + hh, width="0.4064")


def _draw_eagle_inductor(sym: ET.Element, w: int, h: int, pins: list[dict]) -> None:
    """Three humps approximated with short segments."""
    cy = _pin_axis_y(pins, w, h)
    body = max(w * 0.7, 2.2)
    x0 = (w - body) / 2
    x1 = x0 + body
    amp = min(max(h * 0.28, 0.5), 1.2)
    _sym_wire(sym, 0, cy, x0, cy)
    _sym_wire(sym, x1, cy, w, cy)
    # 3 semicircle-ish peaks via 4-point polylines
    seg = body / 3
    for i in range(3):
        a = x0 + i * seg
        m = a + seg / 2
        b = a + seg
        _sym_wire(sym, a, cy, m, cy + amp)
        _sym_wire(sym, m, cy + amp, b, cy)


def _draw_eagle_diode(sym: ET.Element, w: int, h: int, pins: list[dict],
                      led: bool = False) -> None:
    """Triangle + cathode bar, anode left → cathode right."""
    cy = _pin_axis_y(pins, w, h)
    cx = w / 2
    half = min(max(h * 0.35, 0.7), 1.5)
    tip = cx + min(w * 0.18, 0.9)
    base = cx - min(w * 0.22, 1.1)
    _sym_wire(sym, 0, cy, base, cy)
    _sym_wire(sym, tip, cy, w, cy)
    _sym_wire(sym, base, cy - half, base, cy + half)
    _sym_wire(sym, base, cy - half, tip, cy)
    _sym_wire(sym, base, cy + half, tip, cy)
    _sym_wire(sym, tip, cy - half, tip, cy + half, width="0.4064")
    if led:
        # Emission arrows (up-right from body)
        for dy in (0.15, 0.55):
            ax, ay = tip + 0.15, cy + half * 0.2 + dy
            bx, by = tip + 0.7, cy + half * 0.2 + dy + 0.45
            _sym_wire(sym, ax, ay, bx, by)
            _sym_wire(sym, bx, by, bx - 0.25, by - 0.05)
            _sym_wire(sym, bx, by, bx - 0.05, by - 0.25)


def _draw_eagle_transistor(sym: ET.Element, w: int, h: int, kind: str) -> None:
    """
    Discrete FET/BJT in EAGLE coords (y-up).
    Gate/base left, drain/collector top, source/emitter bottom.
    """
    cx, cy = w / 2, -h / 2
    s = min(w, h) * 0.38
    channel_x = cx - s * 0.05
    gate_x = cx - s * 0.55
    right_x = cx + s * 0.45
    top_y = cy + s * 0.85
    bot_y = cy - s * 0.85

    if kind in {"nmos", "pmos"}:
        _sym_wire(sym, 0, cy, gate_x, cy)
        _sym_wire(sym, gate_x, cy - s * 0.55, gate_x, cy + s * 0.55)
        for y0, y1 in (
            (cy - s * 0.55, cy - s * 0.22),
            (cy - s * 0.12, cy + s * 0.12),
            (cy + s * 0.22, cy + s * 0.55),
        ):
            _sym_wire(sym, channel_x, y0, channel_x, y1, width="0.4064")
        # Drain up, source down
        _sym_wire(sym, channel_x, cy + s * 0.55, right_x, cy + s * 0.55)
        _sym_wire(sym, right_x, cy + s * 0.55, right_x, top_y)
        _sym_wire(sym, channel_x, cy - s * 0.55, right_x, cy - s * 0.55)
        _sym_wire(sym, right_x, cy - s * 0.55, right_x, bot_y)
        # Source arrow
        if kind == "nmos":
            _sym_wire(sym, right_x, cy - s * 0.55, channel_x + 0.05, cy - s * 0.55)
            _sym_wire(sym, channel_x + 0.05, cy - s * 0.55,
                      channel_x + 0.35, cy - s * 0.55 + 0.25)
            _sym_wire(sym, channel_x + 0.05, cy - s * 0.55,
                      channel_x + 0.35, cy - s * 0.55 - 0.25)
        else:
            _sym_wire(sym, channel_x, cy - s * 0.55, right_x - 0.05, cy - s * 0.55)
            _sym_wire(sym, right_x - 0.05, cy - s * 0.55,
                      right_x - 0.35, cy - s * 0.55 + 0.25)
            _sym_wire(sym, right_x - 0.05, cy - s * 0.55,
                      right_x - 0.35, cy - s * 0.55 - 0.25)
        # Body diode stub
        mid = right_x + s * 0.25
        _sym_wire(sym, right_x, cy + s * 0.55, mid, cy + s * 0.55)
        _sym_wire(sym, mid, cy + s * 0.55, mid, cy - s * 0.55)
        _sym_wire(sym, mid, cy - s * 0.55, right_x, cy - s * 0.55)
        if kind == "nmos":
            _sym_wire(sym, mid - 0.2, cy - 0.15, mid + 0.2, cy - 0.15)
            _sym_wire(sym, mid - 0.2, cy - 0.15, mid, cy + 0.2)
            _sym_wire(sym, mid + 0.2, cy - 0.15, mid, cy + 0.2)
            _sym_wire(sym, mid - 0.2, cy + 0.2, mid + 0.2, cy + 0.2, width="0.4064")
        else:
            _sym_wire(sym, mid - 0.2, cy + 0.15, mid + 0.2, cy + 0.15)
            _sym_wire(sym, mid - 0.2, cy + 0.15, mid, cy - 0.2)
            _sym_wire(sym, mid + 0.2, cy + 0.15, mid, cy - 0.2)
            _sym_wire(sym, mid - 0.2, cy - 0.2, mid + 0.2, cy - 0.2, width="0.4064")
    else:
        bar_x = cx - s * 0.15
        _sym_wire(sym, bar_x, cy - s * 0.55, bar_x, cy + s * 0.55, width="0.508")
        _sym_wire(sym, 0, cy, bar_x, cy)
        _sym_wire(sym, bar_x, cy + s * 0.28, right_x, top_y)
        _sym_wire(sym, bar_x, cy - s * 0.28, right_x, bot_y)
        # Emitter arrow
        if kind == "npn":
            _sym_wire(sym, bar_x + 0.15, cy - s * 0.2, right_x, bot_y)
            _sym_wire(sym, right_x, bot_y, right_x - 0.35, bot_y + 0.35)
            _sym_wire(sym, right_x, bot_y, right_x - 0.45, bot_y + 0.1)
        else:
            _sym_wire(sym, right_x, bot_y, bar_x + 0.15, cy - s * 0.2)
            _sym_wire(sym, bar_x + 0.15, cy - s * 0.2,
                      bar_x + 0.5, cy - s * 0.2 - 0.3)
            _sym_wire(sym, bar_x + 0.15, cy - s * 0.2,
                      bar_x + 0.55, cy - s * 0.05)


def _add_symbol(
    symbols: ET.Element, name: str, w: int, h: int, pins: list[dict],
    comp_type: str = "",
) -> None:
    """
    Build an EAGLE symbol. Common passives/FETs get standard glyphs so DipTrace
    import matches the SVG schematic; everything else stays a pin-box.
    """
    sym = _sub(symbols, "symbol", name=name)
    t = (comp_type or "").lower()
    kind = svg_generator._transistor_kind(t)

    if "resistor" in t:
        _draw_eagle_resistor(sym, w, h, pins)
    elif "capacitor" in t or t in {"cap", "bypass"}:
        _draw_eagle_capacitor(sym, w, h, pins)
    elif "inductor" in t or "coil" in t or "ferrite" in t:
        _draw_eagle_inductor(sym, w, h, pins)
    elif "led" in t:
        _draw_eagle_diode(sym, w, h, pins, led=True)
    elif any(x in t for x in ("diode", "schottky", "zener", "tvs")) and len(pins) <= 2:
        _draw_eagle_diode(sym, w, h, pins)
    elif kind and len(pins) <= 4:
        _draw_eagle_transistor(sym, w, h, kind)
    else:
        _sym_body_rect(sym, w, h)

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


# member: (part, pin_name, (x, y), side)  — coords in grid steps, y downward
_Member = tuple[str, str, tuple[int, int], str]
_Wire = tuple[int, int, int, int]  # x1, y1, x2, y2 in grid steps
_Pt = tuple[int, int]
_RectI = tuple[int, int, int, int]  # x1, y1, x2, y2 body obstacle

_STUB_DIR = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
_WIRE_W = "0.1524"
_NET_LAYER = "91"
_ROUTE_PASSES = 3
_BODY_PAD = 1  # grid steps of clearance around foreign component bodies


def _lane_exit(x: int, y: int, side: str) -> _Pt:
    dx, dy = _STUB_DIR.get(side, (1, 0))
    return (x + dx * STUB_LEN, y + dy * STUB_LEN)


def _add_seg(wires: list[_Wire], a: _Pt, b: _Pt) -> None:
    """Append an axis-aligned segment if a and b differ."""
    if a == b:
        return
    ax, ay = a
    bx, by = b
    if ax == bx or ay == by:
        wires.append((ax, ay, bx, by))
        return
    # Should not happen for Manhattan paths; force L via horizontal first.
    wires.append((ax, ay, bx, ay))
    wires.append((bx, ay, bx, by))


def _h_hits_rect(y: int, x1: int, x2: int, rect: _RectI) -> bool:
    rx1, ry1, rx2, ry2 = rect
    if y <= ry1 or y >= ry2:
        return False
    return max(x1, x2) > rx1 and min(x1, x2) < rx2


def _v_hits_rect(x: int, y1: int, y2: int, rect: _RectI) -> bool:
    rx1, ry1, rx2, ry2 = rect
    if x <= rx1 or x >= rx2:
        return False
    return max(y1, y2) > ry1 and min(y1, y2) < ry2


def _seg_hits_obstacles(a: _Pt, b: _Pt, obstacles: list[_RectI]) -> bool:
    ax, ay = a
    bx, by = b
    if ay == by:
        return any(_h_hits_rect(ay, ax, bx, r) for r in obstacles)
    if ax == bx:
        return any(_v_hits_rect(ax, ay, by, r) for r in obstacles)
    mid = (bx, ay)
    return _seg_hits_obstacles(a, mid, obstacles) or _seg_hits_obstacles(mid, b, obstacles)


def _wires_hit_obstacles(wires: list[_Wire], obstacles: list[_RectI]) -> bool:
    return any(
        _seg_hits_obstacles((x1, y1), (x2, y2), obstacles)
        for x1, y1, x2, y2 in wires
    )


class _GridRouter:
    """Tracks placed wire segments and finds obstacle-clear Manhattan paths."""

    def __init__(self) -> None:
        self._h: list[tuple[int, int, int]] = []  # (y, xmin, xmax)
        self._v: list[tuple[int, int, int]] = []  # (x, ymin, ymax)

    def register(self, wires: list[_Wire]) -> None:
        for x1, y1, x2, y2 in wires:
            if y1 == y2 and x1 != x2:
                self._h.append((y1, min(x1, x2), max(x1, x2)))
            elif x1 == x2 and y1 != y2:
                self._v.append((x1, min(y1, y2), max(y1, y2)))

    def _h_conflict(self, y: int, x1: int, x2: int) -> bool:
        xmin, xmax = min(x1, x2), max(x1, x2)
        if xmin == xmax:
            return False
        for ey, exmin, exmax in self._h:
            if ey == y and xmin < exmax and xmax > exmin:
                return True
        return False

    def _v_conflict(self, x: int, y1: int, y2: int) -> bool:
        ymin, ymax = min(y1, y2), max(y1, y2)
        if ymin == ymax:
            return False
        for ex, eymin, eymax in self._v:
            if ex == x and ymin < eymax and ymax > eymin:
                return True
        return False

    def _wires_conflict(self, wires: list[_Wire]) -> bool:
        for x1, y1, x2, y2 in wires:
            if y1 == y2:
                if self._h_conflict(y1, x1, x2):
                    return True
            elif x1 == x2:
                if self._v_conflict(x1, y1, y2):
                    return True
        return False

    def path_ok(self, wires: list[_Wire], obstacles: list[_RectI]) -> bool:
        if obstacles and _wires_hit_obstacles(wires, obstacles):
            return False
        return not self._wires_conflict(wires)

    def manhattan(self, a: _Pt, b: _Pt, obstacles: list[_RectI], max_tries: int = 12) -> list[_Wire]:
        """Obstacle- and wire-aware Manhattan path from a to b."""
        if a == b:
            return []
        ax, ay = a
        bx, by = b

        def try_wires(cand: list[_Wire]) -> list[_Wire] | None:
            if self.path_ok(cand, obstacles):
                self.register(cand)
                return cand
            return None

        # Direct H / V
        if ay == by or ax == bx:
            direct: list[_Wire] = []
            _add_seg(direct, a, b)
            got = try_wires(direct)
            if got is not None:
                return got
            for k in range(1, max_tries + 1):
                for sign in (1, -1):
                    detour: list[_Wire] = []
                    if ay == by:
                        my = ay + sign * k
                        _add_seg(detour, a, (ax, my))
                        _add_seg(detour, (ax, my), (bx, my))
                        _add_seg(detour, (bx, my), b)
                    else:
                        mx = ax + sign * k
                        _add_seg(detour, a, (mx, ay))
                        _add_seg(detour, (mx, ay), (mx, by))
                        _add_seg(detour, (mx, by), b)
                    got = try_wires(detour)
                    if got is not None:
                        return got
            fallback: list[_Wire] = []
            _add_seg(fallback, a, b)
            self.register(fallback)
            return fallback

        offsets = [0]
        for k in range(1, max_tries):
            offsets += [k, -k]

        # H-then-V via mid-x; V-then-H via mid-y
        base_mx = (ax + bx) // 2
        base_my = (ay + by) // 2
        for off in offsets:
            mx = base_mx + off
            cand = []
            _add_seg(cand, a, (mx, ay))
            _add_seg(cand, (mx, ay), (mx, by))
            _add_seg(cand, (mx, by), b)
            got = try_wires(cand)
            if got is not None:
                return got

            my = base_my + off
            cand = []
            _add_seg(cand, a, (ax, my))
            _add_seg(cand, (ax, my), (bx, my))
            _add_seg(cand, (bx, my), b)
            got = try_wires(cand)
            if got is not None:
                return got

        # U-detours around the endpoint bounding box
        for k in range(1, max_tries + 1):
            top, bot = min(ay, by) - k, max(ay, by) + k
            left, right = min(ax, bx) - k, max(ax, bx) + k
            for via in (
                [(ax, top), (bx, top)],
                [(ax, bot), (bx, bot)],
                [(left, ay), (left, by)],
                [(right, ay), (right, by)],
            ):
                cand = []
                _add_seg(cand, a, via[0])
                _add_seg(cand, via[0], via[1])
                _add_seg(cand, via[1], b)
                got = try_wires(cand)
                if got is not None:
                    return got

        fallback = []
        _add_seg(fallback, a, (bx, ay))
        _add_seg(fallback, (bx, ay), b)
        self.register(fallback)
        return fallback


def _route_two_pin(
    m1: _Member,
    m2: _Member,
    router: _GridRouter,
    obstacles: list[_RectI],
    max_tries: int = 12,
) -> list[_Wire]:
    """Stub out from each pin tip, then Manhattan between lane exits."""
    _, _, (x1, y1), side1 = m1
    _, _, (x2, y2), side2 = m2
    lane1 = _lane_exit(x1, y1, side1)
    lane2 = _lane_exit(x2, y2, side2)
    wires: list[_Wire] = []
    stubs: list[_Wire] = []
    _add_seg(stubs, (x1, y1), lane1)
    _add_seg(stubs, lane2, (x2, y2))
    # Stubs are short breakouts; register them then route between lanes
    router.register(stubs)
    mid = router.manhattan(lane1, lane2, obstacles, max_tries=max_tries)
    wires.extend(stubs)
    wires.extend(mid)
    return wires


def _trunk_route(
    members: list[_Member],
    router: _GridRouter,
    obstacles: list[_RectI],
    trunk_y: int | None = None,
    max_tries: int = 20,
) -> tuple[list[_Wire], list[_Pt]]:
    """
    Horizontal trunk with stubs from each pin tip via its lane exit.
    Chooses a clear trunk_y when not forced (power rails).
    Returns (wires, junction points on the trunk).
    """
    if not members:
        return [], []

    lanes: list[_Pt] = []
    tips: list[_Pt] = []
    for _, _, (x, y), side in members:
        tips.append((x, y))
        lanes.append(_lane_exit(x, y, side))

    trunk_xs = sorted({lx for lx, _ in lanes})
    x0, x1 = trunk_xs[0], trunk_xs[-1]

    if trunk_y is None:
        ys = sorted(ly for _, ly in lanes)
        base = ys[len(ys) // 2]
        chosen = base
        for k in range(max_tries):
            for ty in ([base] if k == 0 else [base + k, base - k]):
                trunk_w: list[_Wire] = []
                if x0 != x1:
                    _add_seg(trunk_w, (x0, ty), (x1, ty))
                branch_ok = True
                branches: list[_Wire] = []
                for lx, ly in lanes:
                    br: list[_Wire] = []
                    _add_seg(br, (lx, ly), (lx, ty))
                    if not router.path_ok(br, obstacles):
                        branch_ok = False
                        break
                    branches.extend(br)
                if not branch_ok:
                    continue
                if trunk_w and not router.path_ok(trunk_w, obstacles):
                    continue
                chosen = ty
                break
            else:
                continue
            break
        trunk_y = chosen

    wires: list[_Wire] = []
    junctions: list[_Pt] = []

    for tip, lane in zip(tips, lanes):
        lx, ly = lane
        _add_seg(wires, tip, lane)
        join = (lx, trunk_y)
        _add_seg(wires, lane, join)
        junctions.append(join)

    if len(trunk_xs) >= 2:
        _add_seg(wires, (trunk_xs[0], trunk_y), (trunk_xs[-1], trunk_y))

    router.register(wires)

    if len(members) < 3:
        junctions = []
    return wires, junctions


def _power_trunk_y(
    name: str,
    power_rails: set[str],
    all_tips_y: list[int],
) -> int | None:
    """Dedicated bus Y for VCC-type rails; GND uses local symbols instead."""
    upper = name.upper()
    rail_map = {r.upper(): r for r in power_rails}
    if upper not in rail_map:
        return None
    if svg_generator._is_gnd_net(upper):
        return None

    ymin = min(all_tips_y) if all_tips_y else 0
    vcc_u = sorted(r.upper() for r in power_rails if not svg_generator._is_gnd_net(r))
    spacing = 4  # grid steps between parallel rails
    if upper in vcc_u:
        return ymin - STUB_LEN - 2 - vcc_u.index(upper) * spacing
    return None


def _add_gnd_supply(
    packages: ET.Element,
    symbols: ET.Element,
    devicesets: ET.Element,
    net_name: str,
) -> str:
    """
    Add a one-pin IEEE ground supply device for ``net_name``.
    Returns the deviceset name. Safe to call once per distinct ground net.
    """
    ds_name = _ident(net_name, "GND")
    pin_name = ds_name
    pkg_name = f"SUP_{ds_name}"

    # Package: single dummy pad (schematic-only supply)
    pkg = _sub(packages, "package", name=pkg_name)
    _sub(pkg, "smd", name="1", x="0", y="0", dx="1.27", dy="1.27", layer="1")
    _sub(pkg, "text", x="0", y=_mm(1), size="1.27", layer="25").text = ">NAME"

    # Symbol: three decreasing bars below a point-length supply pin at origin
    sym = _sub(symbols, "symbol", name=ds_name)
    # Stem from pin to first bar
    _sub(sym, "wire", x1="0", y1="0", x2="0", y2=_mm(-0.5),
         width="0.1524", layer="94")
    for i, half_w in enumerate([0.75, 0.5, 0.25]):  # grid steps
        yy = -(1 + i)  # 1, 2, 3 grid steps below origin
        _sub(sym, "wire",
             x1=_mm(-half_w), y1=_mm(yy),
             x2=_mm(half_w), y2=_mm(yy),
             width="0.254", layer="94")
    _sub(sym, "text", x="0", y=_mm(-4.2), size="1.27", layer="95",
         align="center").text = net_name
    _sub(sym, "pin", name=pin_name, x="0", y="0", visible="off",
         length="point", direction="sup", rot="R270")

    ds = _sub(devicesets, "deviceset", name=ds_name, prefix="GND", uservalue="no")
    _sub(ds, "description").text = f"Ground supply ({net_name})"
    _sub(_sub(ds, "gates"), "gate", name=GATE, symbol=ds_name, x="0", y="0")
    dev = _sub(_sub(ds, "devices"), "device", name="", package=pkg_name)
    _sub(_sub(dev, "connects"), "connect", gate=GATE, pin=pin_name, pad="1")
    _sub(_sub(dev, "technologies"), "technology", name="")
    return ds_name


_SUPPLY_ROT = {
    "bottom": "R0",    # symbol bars hang down (default)
    "top": "R180",     # flip so bars go up
    "left": "R270",    # bars go left (EAGLE CCW rot)
    "right": "R90",    # bars go right
}


def _emit_gnd_local_net(
    nets_el: ET.Element,
    parts: ET.Element,
    instances: ET.Element,
    name: str,
    members: list[_Member],
    deviceset: str,
    pin_name: str,
) -> None:
    """
    Emit a ground net as independent segments: each component pin stubs out to
    its own ground-supply instance (no shared ground bar).
    """
    net_el = _sub(nets_el, "net", name=name, class_="0")
    for i, (part, pin, (x, y), side) in enumerate(members, start=1):
        gnd_part = _ident(f"{deviceset}_{part}_{i}", f"GND{i}")
        _sub(parts, "part", name=gnd_part, library=LIB, deviceset=deviceset,
             device="", value=name)
        lx, ly = _lane_exit(x, y, side)
        _sub(instances, "instance", part=gnd_part, gate=GATE,
             x=_mm(lx), y=_mm(-ly), rot=_SUPPLY_ROT.get(side, "R0"))

        seg = _sub(net_el, "segment")
        _sub(seg, "pinref", part=part, gate=GATE, pin=pin)
        _sub(seg, "pinref", part=gnd_part, gate=GATE, pin=pin_name)
        _write_wires(seg, [(x, y, lx, ly)] if (x, y) != (lx, ly) else [])


def _write_wires(seg: ET.Element, wires: list[_Wire]) -> None:
    for x1, y1, x2, y2 in wires:
        _sub(seg, "wire",
             x1=_mm(x1), y1=_mm(-y1), x2=_mm(x2), y2=_mm(-y2),
             width=_WIRE_W, layer=_NET_LAYER)


def _body_rects_for_parts(
    part_bodies: dict[str, _RectI],
    members: list[_Member],
) -> list[_RectI]:
    """Obstacle rects for component bodies this net does not attach to."""
    attached = {part for part, _, _, _ in members}
    return [rect for part, rect in part_bodies.items() if part not in attached]


def _route_net_wires(
    members: list[_Member],
    router: _GridRouter,
    obstacles: list[_RectI],
    trunk_y: int | None = None,
    max_tries: int = 12,
) -> tuple[list[_Wire], list[_Pt], _Pt]:
    """
    Compute wires/junctions/label position for one net.
    Returns (wires, junctions, label_at).
    """
    if len(members) == 1:
        _, _, (x, y), side = members[0]
        lane = _lane_exit(x, y, side)
        wires: list[_Wire] = []
        _add_seg(wires, (x, y), lane)
        router.register(wires)
        return wires, [], lane

    if len(members) == 2 and trunk_y is None:
        wires = _route_two_pin(
            members[0], members[1], router, obstacles, max_tries=max_tries,
        )
        _, _, (x1, y1), s1 = members[0]
        _, _, (x2, y2), s2 = members[1]
        l1 = _lane_exit(x1, y1, s1)
        l2 = _lane_exit(x2, y2, s2)
        return wires, [], ((l1[0] + l2[0]) // 2, (l1[1] + l2[1]) // 2)

    wires, junctions = _trunk_route(
        members, router, obstacles, trunk_y=trunk_y, max_tries=max_tries + 8,
    )
    xs = [m[2][0] for m in members]
    ty = trunk_y if trunk_y is not None else (
        sorted(_lane_exit(*m[2], m[3])[1] for m in members)[len(members) // 2]
    )
    # Prefer actual trunk Y from a junction if present
    if junctions:
        ty = junctions[0][1]
    return wires, junctions, ((min(xs) + max(xs)) // 2, ty)


def _multi_pass_route_eagle(
    jobs: list[dict],
    part_bodies: dict[str, _RectI],
    passes: int = _ROUTE_PASSES,
) -> list[tuple[list[_Wire], list[_Pt], _Pt]]:
    """
    Route all non-GND nets ``passes`` times, avoiding foreign component bodies
    and other traces. Dirty (still colliding) nets are re-routed first on
    subsequent passes with a wider search.
    """
    n = len(jobs)
    if n == 0:
        return []

    results: list[tuple[list[_Wire], list[_Pt], _Pt]] = [
        ([], [], (0, 0)) for _ in range(n)
    ]
    dirty = set(range(n))

    for pass_i in range(passes):
        router = _GridRouter()
        max_tries = 12 + pass_i * 4
        dirty_list = sorted(dirty)
        clean = [i for i in range(n) if i not in dirty]
        if pass_i % 2 == 1:
            dirty_list = list(reversed(dirty_list))
            clean = list(reversed(clean))
        order = dirty_list + clean

        new_results: list[tuple[list[_Wire], list[_Pt], _Pt] | None] = [None] * n
        for i in order:
            job = jobs[i]
            obstacles = _body_rects_for_parts(part_bodies, job["members"])
            new_results[i] = _route_net_wires(
                job["members"],
                router,
                obstacles,
                trunk_y=job.get("trunk_y"),
                max_tries=max_tries,
            )
        results = [r for r in new_results]  # type: ignore[misc]

        dirty = set()
        for i, job in enumerate(jobs):
            wires, _, _ = results[i]
            obstacles = _body_rects_for_parts(part_bodies, job["members"])
            if obstacles and _wires_hit_obstacles(wires, obstacles):
                dirty.add(i)

    return results


def _emit_net(
    nets_el: ET.Element,
    name: str,
    members: list[_Member],
    wires: list[_Wire],
    junctions: list[_Pt],
    label_at: _Pt,
) -> None:
    """
    Emit one <net> with a single <segment> containing all pinrefs, routed
    wires, junctions (for trunks), and one label.
    """
    net_el = _sub(nets_el, "net", name=name, class_="0")
    seg = _sub(net_el, "segment")

    for part, pin_name, _, _ in members:
        _sub(seg, "pinref", part=part, gate=GATE, pin=pin_name)

    _write_wires(seg, wires)
    for jx, jy in junctions:
        _sub(seg, "junction", x=_mm(jx), y=_mm(-jy))

    _sub(seg, "label", x=_mm(label_at[0]), y=_mm(-label_at[1]),
         size="1.778", layer="95", rot="R0")


def generate_eagle_sch(circuit: dict, bom_rows: list[dict] | None = None) -> str:
    """
    Convert a circuit dict to an EAGLE XML schematic string.

    bom_rows: optional rows from bom_report.json. When a component ID matches a
    BOM RefDes, the BOM pattern is used as the package name and the MPN is
    attached as an attribute, so DipTrace can match it to your pattern library.
    """
    components = copy.deepcopy(circuit.get("components", []))
    svg_generator._normalize_common_bodies(components)
    nets = circuit.get("nets", [])
    power_rails = set(circuit.get("power_rails", []) or [])
    bonded = svg_generator.place_support_parts(components, nets, power_rails)
    svg_generator._resolve_overlaps(components, bonded_pairs=bonded)
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
    # part name -> body rect in grid steps (obstacle for foreign nets)
    part_bodies: dict[str, _RectI] = {}

    for comp in components:
        cid = str(comp.get("id", "")).strip()
        if not cid:
            continue
        part = _ident(cid, "U")
        w, h, pins = _layout_pins(comp)
        row = bom.get(cid.upper(), {})

        dev_name = f"{_ident(comp.get('name') or comp.get('type'), 'PART')}_{part}"
        _add_symbol(symbols, dev_name, w, h, pins, comp_type=str(comp.get("type") or ""))

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
        part_bodies[part] = (
            ox - _BODY_PAD, oy - _BODY_PAD,
            ox + w + _BODY_PAD, oy + h + _BODY_PAD,
        )

        for p in pins:
            tx, ty = p["tip"]
            pin_index[(cid, p["number"])] = (part, p["name"], (ox + tx, oy + ty), p["side"])

    power_rails = set(circuit.get("power_rails", []) or [])
    # Case-insensitive set of ground rail names present in this circuit
    gnd_rail_names = {r for r in power_rails if svg_generator._is_gnd_net(r)}
    gnd_devices: dict[str, str] = {}  # upper net name -> deviceset name

    merged: dict[str, list[_Member]] = {}
    for i, net in enumerate(circuit.get("nets", []), start=1):
        # Preserve FirstLetterCaps_Signal net names for DipTrace readability.
        name = _ident(net.get("name"), f"N${i}", upper=False)
        for conn in net.get("connections", []):
            key = (str(conn.get("component_id", "")).strip(), str(conn.get("pin_number", "")))
            if key in pin_index:
                merged.setdefault(name, []).append(pin_index[key])

    all_tips_y = [tip[1] for _, _, tip, _ in pin_index.values()]
    gnd_upper = {r.upper() for r in gnd_rail_names}

    # GND nets: local symbols (no shared routing). Signal / VCC: multi-pass route.
    route_jobs: list[dict] = []
    for name, members in merged.items():
        if svg_generator._is_gnd_net(name) or name.upper() in gnd_upper:
            key = name.upper()
            if key not in gnd_devices:
                gnd_devices[key] = _add_gnd_supply(
                    packages, symbols, devicesets, name)
            ds = gnd_devices[key]
            _emit_gnd_local_net(
                nets_el, parts, instances, name, members,
                deviceset=ds, pin_name=_ident(name, "GND"),
            )
            continue
        route_jobs.append({
            "name": name,
            "members": members,
            "trunk_y": _power_trunk_y(name, power_rails, all_tips_y),
        })

    routed = _multi_pass_route_eagle(route_jobs, part_bodies)
    for job, (wires, junctions, label_at) in zip(route_jobs, routed):
        _emit_net(nets_el, job["name"], job["members"], wires, junctions, label_at)

    ET.indent(root)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )
