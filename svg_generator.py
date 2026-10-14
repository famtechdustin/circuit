"""
svg_generator.py — Generate a 2D schematic SVG from a circuit JSON dict.

Components are drawn as labeled rectangles with pin stubs on the specified side.
Nets are drawn as right-angle (Manhattan) wires between pin endpoints.
VCC-type power rails are drawn as horizontal buses; GND nets use a local
ground symbol at each connected pin (no shared ground bar).
"""

import colorsys
import copy
import io
import re
import svgwrite
from svgwrite import cm, mm

CANVAS_W = 2000
CANVAS_H = 1400
PIN_STUB = 14       # px: length of pin stub protruding from component body
FONT_SIZE = 10
LABEL_FONT = 11
PIN_FONT = 8
COMP_FILL = "#eef2ff"
COMP_STROKE = "#334466"
COMP_STROKE_W = 1.5
WIRE_W = 1.5
# Defaults kept for fallbacks / mid anchors
POWER_STROKE = "#cc2200"   # ~5V mid red
GND_STROKE = "#222222"     # primary GND
NET_LABEL_FILL = "#ffffff"
NET_LABEL_STROKE = "#888888"
GRID = 10
WIRE_SPACING = 12   # px to shift mid-bend when a conflict is detected
SEG_TOL = 3.0       # px tolerance for "same axis" overlap check
LANE_STEP = 14      # px per lane index for the dedicated breakout
MAX_LANES = 6       # cap: lane distance cycles after this many pins (max extra = 84 px)
MIN_COMP_H = 90          # minimum height for multi-pin / IC bodies (3 text zones)
MIN_COMP_H_COMMON = 45   # half of MIN_COMP_H — enough for 2-pin series parts
# Clearance added around each component body when checking for overlaps.
# Must cover pin stubs (PIN_STUB) + max lane exit (MAX_LANES * LANE_STEP) + routing room.
COMP_PADDING = PIN_STUB + MAX_LANES * LANE_STEP + 20   # ≈ 118 px
BODY_PAD = 6        # px margin around component bodies that wires must not cross
BODY_CLEARANCE = 8  # min gap between component body boxes (hard non-overlap)
ROUTE_PASSES = 3    # post-layout clearance / reroute iterations
RAIL_TOP_START = 35          # Y of topmost VCC-type rail
RAIL_BOT_START = CANVAS_H - 35  # Y of bottommost GND-type rail
RAIL_SPACING = 22            # px between stacked rails of same polarity
SUPPORT_GAP = 50             # px from host pin tip to nearest edge of support body
SUPPORT_STACK_PAD = 20       # px between stacked supports on the same pin

# Left-side BOM column (refdes + part info)
BOM_MARGIN = 12              # px inset from SVG left edge
BOM_TOP = 28                 # px Y of BOM title
BOM_GAP = 24                 # px between BOM panel and schematic
BOM_PAD_X = 10               # inner horizontal padding
BOM_PAD_Y = 8                # inner vertical padding
BOM_ROW_H = 16               # px per data row
BOM_HEADER_H = 18            # px header row
BOM_TITLE_H = 22             # px "BOM" title band
BOM_FONT = 10
BOM_HEADER_FONT = 10
BOM_TITLE_FONT = 12
BOM_COL_GAP = 12             # px between columns
BOM_MIN_W = 180
BOM_FILL = "#ffffff"
BOM_STROKE = "#99a3b5"
BOM_HEADER_FILL = "#eef2ff"

SIGNAL_PALETTE_SIZE = 20
SIGNAL_PALETTE_OFFSET = 7    # shift on each 20-pin cycle (pins 21–40, …)

_GND_NAMES = {"GND", "AGND", "DGND", "PGND", "0V", "ISO_GND"}
_GND_STROKES = {
    "GND": "#222222",
    "0V": "#222222",
    "PGND": "#3a3a3a",
    "DGND": "#555555",
    "AGND": "#6e6e6e",
    "ISO_GND": "#999999",
}
# One-in / one-out series parts — layout stacks these beside host pins.
_PASSIVE_TYPE_TOKENS = (
    "resistor", "capacitor", "cap", "bypass",
    "inductor", "coil", "ferrite", "bead",
    "diode", "led", "schottky", "zener", "tvs",
)
_OPPOSITE_SIDE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}

# Voltage tokens: "3V3", "3.3V", "12V", optional leading digits in names like "MCU_5V"
_VOLTAGE_RE = re.compile(
    r"(?:(?P<a>\d+)V(?P<b>\d+)\b)|(?:(?P<c>\d+(?:\.\d+)?)\s*V\b)",
    re.IGNORECASE,
)


def _hsl_to_hex(h: float, s: float, lightness: float) -> str:
    """h in degrees [0,360), s/l in [0,1] → #rrggbb."""
    r, g, b = colorsys.hls_to_rgb(h / 360.0, lightness, s)
    return f"#{int(r * 255 + 0.5):02x}{int(g * 255 + 0.5):02x}{int(b * 255 + 0.5):02x}"


def _build_signal_palette(n: int = SIGNAL_PALETTE_SIZE) -> list[str]:
    """n hues spaced across the gamut, skipping the red band used for power."""
    # Hue range ~35°–330° avoids near-red and stays saturated (not gray).
    if n <= 1:
        return [_hsl_to_hex(210, 0.75, 0.42)]
    lo, hi = 35.0, 330.0
    return [
        _hsl_to_hex(lo + i * (hi - lo) / (n - 1), 0.78, 0.42)
        for i in range(n)
    ]


SIGNAL_PALETTE = _build_signal_palette()
WIRE_STROKE = SIGNAL_PALETTE[0]  # legacy alias: first signal color


def _is_gnd_net(name: str) -> bool:
    u = (name or "").upper().strip()
    if not u:
        return False
    if u in _GND_NAMES:
        return True
    return u.endswith("_GND")


def _gnd_stroke(name: str) -> str:
    """Gray shade for ground nets — darkest for primary GND, lighter for ISO/etc."""
    u = (name or "").upper().strip()
    return _GND_STROKES.get(u, "#999999")


def _parse_voltage(name: str) -> float | None:
    """Extract volts from a rail name (12V, 5V, 3V3, 3.3V). None if unknown."""
    if not name:
        return None
    m = _VOLTAGE_RE.search(name)
    if not m:
        return None
    if m.group("a") is not None:
        return float(f"{m.group('a')}.{m.group('b')}")
    return float(m.group("c"))


def _power_stroke(name: str) -> str:
    """Red shade for power rails — darker red for higher voltage."""
    v = _parse_voltage(name)
    if v is None:
        return POWER_STROKE
    # Anchor band: 3V light → 5V mid → 12V dark (clamp outside)
    t = max(0.0, min(1.0, (v - 3.0) / 9.0))
    lightness = 0.55 - t * 0.32  # ~0.55 at 3V, ~0.48 at 5V, ~0.23 at 12V
    return _hsl_to_hex(0.0, 0.92, lightness)


def _signal_stroke(pin_number: int) -> str:
    """Signal color from pin index; cycles every 20 pins with a palette offset."""
    i = max(0, int(pin_number) - 1)
    slot = (i % SIGNAL_PALETTE_SIZE + (i // SIGNAL_PALETTE_SIZE) * SIGNAL_PALETTE_OFFSET) % SIGNAL_PALETTE_SIZE
    return SIGNAL_PALETTE[slot]


def _as_pin_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _representative_pin(connections: list, pin_counts: dict[str, int]) -> int:
    """Pick a pin number for signal coloring: prefer the largest attached IC."""
    best: tuple[int, int] | None = None  # (pin_count, pin_number)
    fallback: list[int] = []
    for conn in connections or []:
        pn = _as_pin_int(conn.get("pin_number"))
        if pn is None:
            continue
        fallback.append(pn)
        cid = str(conn.get("component_id", "")).strip()
        n_pins = pin_counts.get(cid, 0)
        if best is None or n_pins > best[0] or (n_pins == best[0] and pn < best[1]):
            best = (n_pins, pn)
    if best is not None:
        return best[1]
    return min(fallback) if fallback else 1


def _is_passive_type(comp: dict) -> bool:
    t = (comp.get("type") or "").lower()
    return any(tok in t for tok in _PASSIVE_TYPE_TOKENS)


def _is_common_part(comp: dict) -> bool:
    """2-pin series/common parts: one in, one out — render at half IC height."""
    pins = comp.get("pins") or []
    return len(pins) <= 2 and _is_passive_type(comp)


def _eff_h(comp: dict) -> float:
    """Effective component height — floor differs for common 2-pin vs multi-pin."""
    floor = MIN_COMP_H_COMMON if _is_common_part(comp) else MIN_COMP_H
    return max(float(comp["bounding_box"]["height"]), floor)


def _is_multi_pin_host(comp: dict) -> bool:
    """ICs and other primary parts (3+ pins) that support parts attach to."""
    return len(comp.get("pins") or []) >= 3


def _opposite_side(side: str) -> str:
    return _OPPOSITE_SIDE.get(side, "right")


def _pin_net_map(nets: list) -> dict[tuple[str, str], str]:
    """(component_id, pin_number_str) -> net name."""
    out: dict[tuple[str, str], str] = {}
    for net in nets or []:
        name = net.get("name") or ""
        for conn in net.get("connections") or []:
            cid = str(conn.get("component_id", "")).strip()
            pn = str(conn.get("pin_number", "")).strip()
            if cid and pn:
                out[(cid, pn)] = name
    return out


def _net_members(nets: list) -> dict[str, list[tuple[str, str]]]:
    """net name -> [(component_id, pin_number_str), ...]."""
    out: dict[str, list[tuple[str, str]]] = {}
    for net in nets or []:
        name = net.get("name") or ""
        if not name:
            continue
        members = out.setdefault(name, [])
        for conn in net.get("connections") or []:
            cid = str(conn.get("component_id", "")).strip()
            pn = str(conn.get("pin_number", "")).strip()
            if cid and pn:
                members.append((cid, pn))
    return out


def _coerce_pin_number(host_id: str, pin_number, by_id: dict[str, dict]):
    """Return pin_number typed like the host's pin.number (int if possible)."""
    host = by_id.get(host_id) or {}
    target = str(pin_number).strip()
    for hp in host.get("pins") or []:
        if str(hp.get("number", "")).strip() == target:
            return hp.get("number")
    try:
        return int(target)
    except (TypeError, ValueError):
        return pin_number


def _infer_supports(
    comp: dict,
    by_id: dict[str, dict],
    pin_nets: dict[tuple[str, str], str],
    net_members: dict[str, list[tuple[str, str]]],
    power_rails: set[str],
) -> dict | None:
    """
    Infer supports for a 2-pin passive: one net touches a multi-pin host pin;
    prefer a unique host pin, else a non-rail / power_in candidate.
    """
    pins = comp.get("pins") or []
    if len(pins) != 2:
        return None
    cid = str(comp.get("id", "")).strip()
    if not cid:
        return None

    candidates: list[tuple[str, str, str]] = []
    for pin in pins:
        pn = str(pin.get("number", "")).strip()
        net_name = pin_nets.get((cid, pn))
        if not net_name:
            continue
        for other_cid, other_pn in net_members.get(net_name, []):
            if other_cid == cid:
                continue
            host = by_id.get(other_cid)
            if host and _is_multi_pin_host(host):
                candidates.append((other_cid, other_pn, net_name))

    uniq: dict[tuple[str, str], str] = {}
    for hid, hpn, nname in candidates:
        uniq.setdefault((hid, hpn), nname)
    if not uniq:
        return None

    def _is_railish(n: str) -> bool:
        return n in power_rails or _is_gnd_net(n)

    if len(uniq) == 1:
        (hid, hpn), _nname = next(iter(uniq.items()))
        return {"component_id": hid, "pin_number": _coerce_pin_number(hid, hpn, by_id)}

    scored: list[tuple[int, str, str]] = []
    for (hid, hpn), nname in uniq.items():
        score = 0
        if not _is_railish(nname):
            score += 2
        host = by_id.get(hid) or {}
        for hp in host.get("pins") or []:
            if str(hp.get("number", "")).strip() == str(hpn).strip():
                if (hp.get("direction") or "").lower() in (
                    "power_in", "input", "bidirectional",
                ):
                    score += 1
                break
        scored.append((score, hid, hpn))
    scored.sort(key=lambda t: -t[0])
    _, hid, hpn = scored[0]
    return {"component_id": hid, "pin_number": _coerce_pin_number(hid, hpn, by_id)}


def _facing_support_pin(
    support: dict,
    host_id: str,
    host_pin_number,
    pin_nets: dict[tuple[str, str], str],
    net_members: dict[str, list[tuple[str, str]]],
) -> dict | None:
    """Which support pin shares a net with the host pin being supported."""
    sid = str(support.get("id", "")).strip()
    host_net = pin_nets.get((host_id, str(host_pin_number).strip()))
    pins = support.get("pins") or []
    if host_net:
        for pin in pins:
            pn = str(pin.get("number", "")).strip()
            if pin_nets.get((sid, pn)) == host_net:
                return pin
    for pin in pins:
        pn = str(pin.get("number", "")).strip()
        nname = pin_nets.get((sid, pn))
        if not nname:
            continue
        for c, p in net_members.get(nname, []):
            if c == host_id and str(p) == str(host_pin_number).strip():
                return pin
    return pins[0] if pins else None


def _orient_support_pins(
    support: dict,
    host_side: str,
    facing_pin: dict | None,
) -> None:
    """Face the host-net pin toward the host; other pin outward."""
    facing_side = _opposite_side(host_side)
    outward_side = host_side
    facing_num = str((facing_pin or {}).get("number", "")).strip()
    for pin in support.get("pins") or []:
        if facing_num and str(pin.get("number", "")).strip() == facing_num:
            pin["side"] = facing_side
            pin["offset"] = 0.5
        else:
            pin["side"] = outward_side
            pin["offset"] = 0.5


def _seat_support_at_pin(
    support: dict,
    host: dict,
    host_pin: dict,
    stack_index: int,
) -> None:
    """Place support body just outside the host pin tip, stacked along outward axis."""
    tip_x, tip_y = _pin_endpoint(host, host_pin)
    sw = float(support["bounding_box"]["width"])
    sh = _eff_h(support)
    side = host_pin.get("side", "left")
    step = max(sw, sh) + SUPPORT_STACK_PAD
    gap = SUPPORT_GAP + stack_index * step

    if side == "left":
        x = tip_x - gap - sw
        y = tip_y - sh / 2
    elif side == "right":
        x = tip_x + gap
        y = tip_y - sh / 2
    elif side == "top":
        x = tip_x - sw / 2
        y = tip_y - gap - sh
    else:
        x = tip_x - sw / 2
        y = tip_y + gap

    support.setdefault("position", {})
    support["position"]["x"] = x
    support["position"]["y"] = y


def place_support_parts(
    components: list,
    nets: list,
    power_rails: set[str] | None = None,
) -> set[frozenset]:
    """
    Place decoupling/pull-up style support parts next to the host pin they serve.

    Uses component["supports"] when present; otherwise infers for 2-pin passives.
    Mutates components in place. Returns bonded pairs as frozensets of ids.
    """
    power_rails = set(power_rails or [])
    by_id = {str(c.get("id", "")).strip(): c for c in components if c.get("id")}
    pin_nets = _pin_net_map(nets)
    members = _net_members(nets)
    bonded: set[frozenset] = set()

    bindings: list[tuple[dict, str, object]] = []
    for comp in components:
        cid = str(comp.get("id", "")).strip()
        if not cid:
            continue
        supports = comp.get("supports")
        if isinstance(supports, dict) and supports.get("component_id") is not None:
            hid = str(supports.get("component_id", "")).strip()
            hpn = supports.get("pin_number")
            if hid in by_id and hpn is not None:
                bindings.append((comp, hid, hpn))
            continue
        if not _is_passive_type(comp):
            continue
        if len(comp.get("pins") or []) != 2:
            continue
        inferred = _infer_supports(comp, by_id, pin_nets, members, power_rails)
        if inferred:
            comp["supports"] = inferred
            bindings.append((comp, inferred["component_id"], inferred["pin_number"]))

    groups: dict[tuple[str, str], list[dict]] = {}
    for support, hid, hpn in bindings:
        key = (hid, str(hpn).strip())
        groups.setdefault(key, []).append(support)
        bonded.add(frozenset({str(support.get("id", "")).strip(), hid}))

    for (hid, hpn_str), supports in groups.items():
        host = by_id.get(hid)
        if not host:
            continue
        host_pin = None
        for hp in host.get("pins") or []:
            if str(hp.get("number", "")).strip() == hpn_str:
                host_pin = hp
                break
        if not host_pin:
            continue
        host_side = host_pin.get("side", "left")

        def _stack_key(s: dict):
            val = (s.get("value") or "").lower()
            bulk = 1 if ("uf" in val or "µf" in val) else 0
            return (bulk, str(s.get("id", "")))

        for stack_i, support in enumerate(sorted(supports, key=_stack_key)):
            facing = _facing_support_pin(
                support, hid, host_pin.get("number"), pin_nets, members,
            )
            _orient_support_pins(support, host_side, facing)
            _seat_support_at_pin(support, host, host_pin, stack_i)

    return bonded


def _support_host_maps(
    components: list,
) -> tuple[dict[str, str], dict[str, list[dict]], dict[str, dict]]:
    """support_id -> host_id, host_id -> [support comps], id -> comp."""
    by_id = {str(c.get("id", "")).strip(): c for c in components if c.get("id")}
    support_of: dict[str, str] = {}
    for c in components:
        sid = str(c.get("id", "")).strip()
        supports = c.get("supports")
        if not sid or not isinstance(supports, dict):
            continue
        hid = str(supports.get("component_id", "")).strip()
        if hid and hid in by_id:
            support_of[sid] = hid
    hosts_supports: dict[str, list[dict]] = {}
    for sid, hid in support_of.items():
        hosts_supports.setdefault(hid, []).append(by_id[sid])
    return support_of, hosts_supports, by_id


def _cluster_member_ids(
    cid: str,
    support_of: dict[str, str],
    hosts_supports: dict[str, list[dict]],
) -> set[str]:
    host_id = support_of.get(cid, cid)
    ids = {host_id}
    for s in hosts_supports.get(host_id, []):
        ids.add(str(s.get("id", "")).strip())
    return ids


# ---------------------------------------------------------------------------
# Geometry helpers — component bodies as wire obstacles
# ---------------------------------------------------------------------------

_Rect = tuple[float, float, float, float]  # x1, y1, x2, y2
_PtF = tuple[float, float]


def _comp_body_rect(comp: dict, pad: float = BODY_PAD) -> _Rect:
    """Axis-aligned component body expanded by ``pad`` (wires must stay outside)."""
    x = float(comp["position"]["x"])
    y = float(comp["position"]["y"])
    w = float(comp["bounding_box"]["width"])
    h = _eff_h(comp)
    return (x - pad, y - pad, x + w + pad, y + h + pad)


def _obstacles_for_net(components: list, attached_ids: set[str]) -> list[_Rect]:
    """Bodies of components this net does not attach to."""
    return [
        _comp_body_rect(c)
        for c in components
        if str(c.get("id", "")) not in attached_ids
    ]


def _h_hits_rect(y: float, x1: float, x2: float, rect: _Rect) -> bool:
    rx1, ry1, rx2, ry2 = rect
    if y <= ry1 or y >= ry2:
        return False
    xmin, xmax = min(x1, x2), max(x1, x2)
    return xmax > rx1 and xmin < rx2


def _v_hits_rect(x: float, y1: float, y2: float, rect: _Rect) -> bool:
    rx1, ry1, rx2, ry2 = rect
    if x <= rx1 or x >= rx2:
        return False
    ymin, ymax = min(y1, y2), max(y1, y2)
    return ymax > ry1 and ymin < ry2


def _seg_hits_obstacles(
    a: _PtF, b: _PtF, obstacles: list[_Rect], tol: float = SEG_TOL,
) -> bool:
    x1, y1 = a
    x2, y2 = b
    if abs(y1 - y2) < tol:  # horizontal
        return any(_h_hits_rect(y1, x1, x2, r) for r in obstacles)
    if abs(x1 - x2) < tol:  # vertical
        return any(_v_hits_rect(x1, y1, y2, r) for r in obstacles)
    # Non-Manhattan: treat as H then V
    mid = (x2, y1)
    return (
        _seg_hits_obstacles(a, mid, obstacles, tol)
        or _seg_hits_obstacles(mid, b, obstacles, tol)
    )


def _path_hits_obstacles(
    pts: list[_PtF], obstacles: list[_Rect], tol: float = SEG_TOL,
) -> bool:
    for i in range(len(pts) - 1):
        if _seg_hits_obstacles(pts[i], pts[i + 1], obstacles, tol):
            return True
    return False


# ---------------------------------------------------------------------------
# Segment registry — tracks axis-aligned wire segments to prevent overlap
# ---------------------------------------------------------------------------

class _SegmentRegistry:
    """
    Keeps a list of horizontal and vertical wire segments already drawn.
    Used to detect overlapping routes and choose an offset bend point.
    Optionally refuses paths that cross component-body obstacles.
    """

    def __init__(self, tol: float = SEG_TOL):
        self._h: list[tuple[float, float, float]] = []  # (y, xmin, xmax)
        self._v: list[tuple[float, float, float]] = []  # (x, ymin, ymax)
        self._tol = tol

    def _h_conflict(self, y: float, x1: float, x2: float) -> bool:
        xmin, xmax = min(x1, x2), max(x1, x2)
        if xmax - xmin < self._tol:   # zero-length, skip
            return False
        for ey, exmin, exmax in self._h:
            if abs(ey - y) < self._tol:
                if xmin < exmax - self._tol and xmax > exmin + self._tol:
                    return True
        return False

    def _v_conflict(self, x: float, y1: float, y2: float) -> bool:
        ymin, ymax = min(y1, y2), max(y1, y2)
        if ymax - ymin < self._tol:
            return False
        for ex, eymin, eymax in self._v:
            if abs(ex - x) < self._tol:
                if ymin < eymax - self._tol and ymax > eymin + self._tol:
                    return True
        return False

    def register_h(self, y: float, x1: float, x2: float) -> None:
        if abs(x2 - x1) >= self._tol:
            self._h.append((y, min(x1, x2), max(x1, x2)))

    def register_v(self, x: float, y1: float, y2: float) -> None:
        if abs(y2 - y1) >= self._tol:
            self._v.append((x, min(y1, y2), max(y1, y2)))

    def register_path(self, pts: list[_PtF]) -> None:
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if abs(y1 - y2) < self._tol:
                self.register_h(y1, x1, x2)
            elif abs(x1 - x2) < self._tol:
                self.register_v(x1, y1, y2)
            else:
                self.register_h(y1, x1, x2)
                self.register_v(x2, y1, y2)

    def _wire_conflict_path(self, pts: list[_PtF]) -> bool:
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if abs(y1 - y2) < self._tol:
                if self._h_conflict(y1, x1, x2):
                    return True
            elif abs(x1 - x2) < self._tol:
                if self._v_conflict(x1, y1, y2):
                    return True
            else:
                if self._h_conflict(y1, x1, x2) or self._v_conflict(x2, y1, y2):
                    return True
        return False

    def path_ok(self, pts: list[_PtF], obstacles: list[_Rect] | None = None) -> bool:
        if obstacles and _path_hits_obstacles(pts, obstacles, self._tol):
            return False
        return not self._wire_conflict_path(pts)

    def route(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        max_tries: int = 9,
        obstacles: list[_Rect] | None = None,
    ) -> list[tuple[float, float]]:
        """
        Return a Manhattan waypoint list from p1 to p2 that avoids registered
        wire segments and optional component-body obstacles. Tries H-then-V
        L-bends, V-then-H L-bends, and U-detours. Registers the chosen path.
        """
        obstacles = obstacles or []
        x1, y1 = p1
        x2, y2 = p2

        def accept(pts: list[_PtF]) -> list[_PtF] | None:
            # Collapse consecutive duplicates
            cleaned: list[_PtF] = [pts[0]]
            for p in pts[1:]:
                if abs(p[0] - cleaned[-1][0]) >= self._tol or abs(p[1] - cleaned[-1][1]) >= self._tol:
                    cleaned.append(p)
            if len(cleaned) < 2:
                cleaned = [p1, p2]
            if self.path_ok(cleaned, obstacles):
                self.register_path(cleaned)
                return cleaned
            return None

        # Pure horizontal / vertical — detour if blocked
        if abs(y1 - y2) < self.tol:
            got = accept([p1, p2])
            if got:
                return got
            for k in range(1, max_tries + 1):
                for sign in (1, -1):
                    my = y1 + sign * k * WIRE_SPACING
                    got = accept([p1, (x1, my), (x2, my), p2])
                    if got:
                        return got
            self.register_h(y1, x1, x2)
            return [p1, p2]

        if abs(x1 - x2) < self.tol:
            got = accept([p1, p2])
            if got:
                return got
            for k in range(1, max_tries + 1):
                for sign in (1, -1):
                    mx = x1 + sign * k * WIRE_SPACING
                    got = accept([p1, (mx, y1), (mx, y2), p2])
                    if got:
                        return got
            self.register_v(x1, y1, y2)
            return [p1, p2]

        offsets = [0]
        for k in range(1, max_tries):
            offsets.append(k * WIRE_SPACING)
            offsets.append(-k * WIRE_SPACING)

        base_mx = (x1 + x2) / 2
        base_my = (y1 + y2) / 2

        # H-V-H via mid-x, then V-H-V via mid-y
        for off in offsets:
            mx = base_mx + off
            got = accept([p1, (mx, y1), (mx, y2), p2])
            if got:
                return got
            my = base_my + off
            got = accept([p1, (x1, my), (x2, my), p2])
            if got:
                return got

        # U-detours: go around above/below/left/right of the bounding box of endpoints
        for k in range(1, max_tries + 1):
            d = k * WIRE_SPACING
            top = min(y1, y2) - d
            bot = max(y1, y2) + d
            left = min(x1, x2) - d
            right = max(x1, x2) + d
            for pts in (
                [p1, (x1, top), (x2, top), p2],
                [p1, (x1, bot), (x2, bot), p2],
                [p1, (left, y1), (left, y2), p2],
                [p1, (right, y1), (right, y2), p2],
            ):
                got = accept(pts)
                if got:
                    return got

        # Fallback: base mid even if conflicted
        fallback = [p1, (base_mx, y1), (base_mx, y2), p2]
        self.register_path(fallback)
        return fallback

    @property
    def tol(self) -> float:
        return self._tol


# ---------------------------------------------------------------------------
# Left-side BOM column
# ---------------------------------------------------------------------------

_REFDES_RE = re.compile(r"^([A-Za-z]+)(\d+)(.*)$")


def _refdes_sort_key(ref: str) -> tuple:
    """Natural sort: R1, R2, R10 (not R1, R10, R2)."""
    m = _REFDES_RE.match((ref or "").strip())
    if m:
        return (m.group(1).upper(), int(m.group(2)), m.group(3))
    return ((ref or "").upper(), 0, "")


def _text_w(text: str, font_size: float) -> float:
    """Approximate monospace text width in px."""
    return max(0, len(text or "")) * font_size * 0.62


def _build_bom_rows(components: list) -> list[dict]:
    """
    One BOM row per component, sorted by RefDes.
    Fields: refdes, value, name, footprint (empty strings allowed).
    """
    rows: list[dict] = []
    for comp in components:
        ref = str(comp.get("id", "")).strip()
        if not ref:
            continue
        value = str(comp.get("value") or "").strip()
        name = str(comp.get("name") or "").strip()
        # Avoid duplicating value in the name column when they match
        if name and value and name.lower() == value.lower():
            name = ""
        if not name:
            name = str(comp.get("type") or "").strip()
        footprint = str(comp.get("footprint") or "").strip()
        rows.append({
            "refdes": ref,
            "value": value,
            "name": name,
            "footprint": footprint,
        })
    rows.sort(key=lambda r: _refdes_sort_key(r["refdes"]))
    return rows


def _bom_column_widths(rows: list[dict]) -> dict[str, float]:
    """Pixel widths for RefDes / Value / Name / Footprint columns."""
    show_fp = any(r.get("footprint") for r in rows)
    keys = ["refdes", "value", "name"] + (["footprint"] if show_fp else [])
    headers = {
        "refdes": "RefDes",
        "value": "Value",
        "name": "Description",
        "footprint": "Footprint",
    }
    widths: dict[str, float] = {}
    for key in keys:
        w = _text_w(headers[key], BOM_HEADER_FONT)
        for row in rows:
            w = max(w, _text_w(str(row.get(key, "")), BOM_FONT))
        # Small minimums so empty columns don't collapse awkwardly
        widths[key] = max(w, 36 if key == "refdes" else 28)
    return widths


def _bom_column_size(rows: list[dict]) -> tuple[float, float]:
    """Return (panel_width, panel_height) for the BOM box."""
    if not rows:
        return (0.0, 0.0)
    widths = _bom_column_widths(rows)
    inner_w = sum(widths.values()) + BOM_COL_GAP * (len(widths) - 1)
    panel_w = max(BOM_MIN_W, BOM_PAD_X * 2 + inner_w)
    panel_h = (
        BOM_TITLE_H
        + BOM_HEADER_H
        + BOM_ROW_H * len(rows)
        + BOM_PAD_Y * 2
    )
    return (panel_w, panel_h)


def _draw_bom_column(
    dwg: svgwrite.Drawing,
    rows: list[dict],
    x: float,
    y: float,
    width: float,
) -> None:
    """Draw a full BOM table (RefDes, Value, Description[, Footprint]) at (x, y)."""
    if not rows:
        return

    widths = _bom_column_widths(rows)
    keys = list(widths.keys())
    headers = {
        "refdes": "RefDes",
        "value": "Value",
        "name": "Description",
        "footprint": "Footprint",
    }
    panel_h = (
        BOM_TITLE_H
        + BOM_HEADER_H
        + BOM_ROW_H * len(rows)
        + BOM_PAD_Y * 2
    )

    group = dwg.add(dwg.g(id="bom"))

    # Panel background
    group.add(dwg.rect(
        insert=(x, y),
        size=(width, panel_h),
        fill=BOM_FILL,
        stroke=BOM_STROKE,
        stroke_width=1,
        rx=3, ry=3,
    ))

    # Title band
    group.add(dwg.rect(
        insert=(x, y),
        size=(width, BOM_TITLE_H),
        fill=BOM_HEADER_FILL,
        stroke=BOM_STROKE,
        stroke_width=1,
        rx=3, ry=3,
    ))
    # Cover bottom radii of title band so it meets the body cleanly
    group.add(dwg.rect(
        insert=(x, y + BOM_TITLE_H - 4),
        size=(width, 4),
        fill=BOM_HEADER_FILL,
        stroke="none",
    ))
    group.add(dwg.text(
        f"BOM  ({len(rows)})",
        insert=(x + BOM_PAD_X, y + BOM_TITLE_H * 0.68),
        font_size=BOM_TITLE_FONT,
        font_family="monospace",
        font_weight="bold",
        fill="#222",
    ))

    # Column x positions
    col_x: dict[str, float] = {}
    cx = x + BOM_PAD_X
    for key in keys:
        col_x[key] = cx
        cx += widths[key] + BOM_COL_GAP

    header_y = y + BOM_TITLE_H + BOM_PAD_Y + BOM_HEADER_FONT
    for key in keys:
        group.add(dwg.text(
            headers[key],
            insert=(col_x[key], header_y),
            font_size=BOM_HEADER_FONT,
            font_family="monospace",
            font_weight="bold",
            fill="#445",
        ))

    # Divider under header
    div_y = y + BOM_TITLE_H + BOM_PAD_Y + BOM_HEADER_H
    group.add(dwg.line(
        start=(x + 4, div_y),
        end=(x + width - 4, div_y),
        stroke=BOM_STROKE,
        stroke_width=0.75,
    ))

    for i, row in enumerate(rows):
        ty = div_y + BOM_ROW_H * (i + 0.72)
        # Subtle zebra striping for long lists
        if i % 2 == 1:
            group.add(dwg.rect(
                insert=(x + 2, div_y + BOM_ROW_H * i),
                size=(width - 4, BOM_ROW_H),
                fill="#f3f5fa",
                stroke="none",
            ))
        for key in keys:
            text = str(row.get(key, "") or "")
            weight = "bold" if key == "refdes" else "normal"
            fill = "#222" if key == "refdes" else "#444"
            group.add(dwg.text(
                text,
                insert=(col_x[key], ty),
                font_size=BOM_FONT,
                font_family="monospace",
                font_weight=weight,
                fill=fill,
            ))


def generate_svg(circuit: dict, min_w: int | None = None, min_h: int | None = None) -> str:
    """
    Convert a circuit dict (matching the JSON schema) to an SVG string.
    Returns the complete SVG as a str.

    min_w / min_h: optional minimum canvas dimensions (px). The canvas will be
    at least this large even if the component layout is smaller.
    """
    # ── Layout computation (all of this must happen before SVG creation) ──────
    components = copy.deepcopy(circuit.get("components", []))
    nets        = circuit.get("nets", [])
    power_rails = set(circuit.get("power_rails", []))
    bonded = place_support_parts(components, nets, power_rails)
    _resolve_overlaps(components, bonded_pairs=bonded)

    vcc_nets = sorted(n for n in power_rails if not _is_gnd_net(n))

    # BOM column on the left — reserve width before shifting the schematic
    bom_rows = _build_bom_rows(components)
    bom_w, bom_h = _bom_column_size(bom_rows)
    left_margin = COMP_PADDING
    if bom_rows:
        left_margin = BOM_MARGIN + bom_w + BOM_GAP + COMP_PADDING

    # Shift every component so the layout clears the top/left margins
    top_margin = RAIL_TOP_START + max(1, len(vcc_nets)) * RAIL_SPACING + 50
    _normalize_to_canvas(components, top_margin=top_margin, left_margin=left_margin)

    # Dynamic canvas: grow to fit the actual component footprint (+ BOM)
    if components:
        _cw = max(c["position"]["x"] + c["bounding_box"]["width"] + COMP_PADDING
                  for c in components)
        _ch = max(c["position"]["y"] + _eff_h(c) + COMP_PADDING
                  for c in components)
    else:
        _cw, _ch = CANVAS_W, CANVAS_H
    if bom_rows:
        _cw = max(_cw, BOM_MARGIN + bom_w + BOM_GAP)
        _ch = max(_ch, BOM_TOP + bom_h)
    canvas_w = max(CANVAS_W, int(_cw) + 10)
    canvas_h = max(CANVAS_H, int(_ch) + 40)  # +40 for title row
    if min_w:
        canvas_w = max(canvas_w, min_w)
    if min_h:
        canvas_h = max(canvas_h, min_h)

    # VCC-type rails only — GND uses local symbols at each pin
    rail_y: dict[str, float] = {}
    for i, n in enumerate(vcc_nets):
        rail_y[n] = RAIL_TOP_START + i * RAIL_SPACING

    # ── SVG creation ─────────────────────────────────────────────────────────
    dwg = svgwrite.Drawing(
        size=(f"{canvas_w}px", f"{canvas_h}px"),
        profile="full",
    )
    dwg.viewbox(0, 0, canvas_w, canvas_h)

    # Background
    dwg.add(dwg.rect(insert=(0, 0), size=(canvas_w, canvas_h), fill="#fafafa"))

    # Grid dots (subtle)
    grid_group = dwg.add(dwg.g(id="grid", opacity="0.15"))
    for gx in range(0, canvas_w, GRID * 5):
        for gy in range(0, canvas_h, GRID * 5):
            grid_group.add(dwg.circle(center=(gx, gy), r=0.8, fill="#999"))

    # Build pin lookups:
    #   pin_endpoints  — stub tip (the visible pin end)
    #   pin_lane_exits — further out by (pin_idx+1)*LANE_STEP, one unique lane per pin
    #   pin_sides      — pin side for orienting local power symbols
    pin_endpoints:   dict[tuple, tuple[float, float]] = {}
    pin_lane_exits:  dict[tuple, tuple[float, float]] = {}
    pin_sides:       dict[tuple, str] = {}
    pin_counts:      dict[str, int] = {}
    for comp in components:
        cid = str(comp.get("id", "")).strip()
        pins = comp.get("pins", []) or []
        if cid:
            pin_counts[cid] = len(pins)
        for pin_idx, pin in enumerate(pins):
            key = (comp["id"], pin["number"])
            pin_endpoints[key]  = _pin_endpoint(comp, pin)
            pin_lane_exits[key] = _pin_lane_exit(comp, pin, pin_idx)
            pin_sides[key] = pin.get("side", "left")

    wire_group      = dwg.add(dwg.g(id="wires"))
    net_label_group = dwg.add(dwg.g(id="net-labels"))

    # Power nets first (buses / local GND), then multi-pass signal routing
    signal_jobs: list[dict] = []

    for net in nets:
        name        = net.get("name", "")
        connections = net.get("connections", [])
        is_power    = name in power_rails

        # Collect (tip, lane_exit, side) for each connection
        conn_pts: list[tuple[tuple, tuple, str]] = []
        attached: set[str] = set()
        for conn in connections:
            cid = conn.get("component_id", "")
            key = (cid, conn.get("pin_number", -1))
            tip  = pin_endpoints.get(key)
            lane = pin_lane_exits.get(key, tip)
            side = pin_sides.get(key, "left")
            if tip:
                conn_pts.append((tip, lane, side))
                if cid:
                    attached.add(str(cid))

        if is_power and not _is_gnd_net(name):
            stroke = _power_stroke(name)
        elif _is_gnd_net(name):
            stroke = _gnd_stroke(name)
        else:
            stroke = _signal_stroke(_representative_pin(connections, pin_counts))

        # Power: GND gets a local symbol at each pin; VCC-type rails use a bus
        if is_power:
            # Dedicated registry so power buses don't starve signal clearance
            # — still register into a throwaway that we discard after drawing.
            preg = _SegmentRegistry()
            if _is_gnd_net(name):
                for tip, _, side in conn_pts:
                    _draw_power_symbol(dwg, wire_group, name, tip[0], tip[1], side)
            else:
                ry = rail_y.get(name)
                bus_pts = [(tip, lane) for tip, lane, _ in conn_pts]
                if ry is not None and bus_pts:
                    _draw_power_bus(dwg, wire_group, net_label_group,
                                    preg, bus_pts, name, ry, stroke)
                else:
                    for tip, _, side in conn_pts:
                        _draw_power_symbol(dwg, wire_group, name, tip[0], tip[1], side)
            continue

        if len(conn_pts) < 2:
            continue

        signal_jobs.append({
            "name": name,
            "stroke": stroke,
            "attached": attached,
            "conn_pts": [(tip, lane) for tip, lane, _ in conn_pts],
        })

    # Post-layout clearance: route signal nets up to ROUTE_PASSES times so
    # traces avoid foreign component bodies and each other.
    routed = _multi_pass_route_signals(components, signal_jobs, pin_endpoints, pin_lane_exits)

    for job, geom in zip(signal_jobs, routed):
        _draw_routed_net(dwg, wire_group, net_label_group, job, geom)
    # Draw components on top of wires
    comp_group = dwg.add(dwg.g(id="components"))
    for comp in components:
        _draw_component(dwg, comp_group, comp)

    # Full BOM list (refdes + part fields) in a left column
    if bom_rows:
        _draw_bom_column(dwg, bom_rows, x=BOM_MARGIN, y=BOM_TOP, width=bom_w)

    # Title block (to the right of the BOM so it stays readable)
    title = circuit.get("metadata", {}).get("title", "Untitled Circuit")
    revision = circuit.get("metadata", {}).get("revision", 1)
    title_x = (BOM_MARGIN + bom_w + BOM_GAP) if bom_rows else 10
    dwg.add(dwg.text(
        f"{title}  Rev.{revision}",
        insert=(title_x, canvas_h - 10),
        font_size=12,
        font_family="monospace",
        fill="#666",
    ))

    buf = io.StringIO()
    dwg.write(buf)
    return buf.getvalue()


def _body_overlap_xy(ci: dict, cj: dict, gap: float = 0.0) -> tuple[float, float]:
    """Return (ox, oy) penetration of body boxes expanded by ``gap``/2 each side.

    Positive ox and oy means the boxes collide (including the minimum gap).
    """
    pad = gap / 2.0
    xi, yi = float(ci["position"]["x"]), float(ci["position"]["y"])
    wi, hi = float(ci["bounding_box"]["width"]), _eff_h(ci)
    xj, yj = float(cj["position"]["x"]), float(cj["position"]["y"])
    wj, hj = float(cj["bounding_box"]["width"]), _eff_h(cj)
    ox = min(xi + wi + pad, xj + wj + pad) - max(xi - pad, xj - pad)
    oy = min(yi + hi + pad, yj + hj + pad) - max(yi - pad, yj - pad)
    return ox, oy


def _push_apart(
    move_a: set[str],
    move_b: set[str],
    ci: dict,
    cj: dict,
    ox: float,
    oy: float,
    move_ids,
) -> None:
    """Push two move-sets apart along the axis of smaller penetration."""
    cxi = float(ci["position"]["x"]) + float(ci["bounding_box"]["width"]) / 2
    cyi = float(ci["position"]["y"]) + _eff_h(ci) / 2
    cxj = float(cj["position"]["x"]) + float(cj["bounding_box"]["width"]) / 2
    cyj = float(cj["position"]["y"]) + _eff_h(cj) / 2

    # If only one side moves, give it the full separation.
    if move_a and move_b:
        share_a = share_b = 0.5
    elif move_a:
        share_a, share_b = 1.0, 0.0
    else:
        share_a, share_b = 0.0, 1.0

    if ox <= oy:
        dist = ox + 1
        if cxi <= cxj:
            move_ids(move_a, -dist * share_a, 0)
            move_ids(move_b, dist * share_b, 0)
        else:
            move_ids(move_a, dist * share_a, 0)
            move_ids(move_b, -dist * share_b, 0)
    else:
        dist = oy + 1
        if cyi <= cyj:
            move_ids(move_a, 0, -dist * share_a)
            move_ids(move_b, 0, dist * share_b)
        else:
            move_ids(move_a, 0, dist * share_a)
            move_ids(move_b, 0, -dist * share_b)


def _resolve_overlaps(
    components: list,
    max_iter: int = 80,
    bonded_pairs: set[frozenset] | None = None,
) -> None:
    """
    Iteratively push overlapping component boxes apart (in-place).

    Two passes of rules each iteration:
      1. Hard body check — every pair of component bodies must stay at least
         BODY_CLEARANCE apart (including host↔support and sibling supports).
      2. Routing clearance — unrelated clusters are also kept COMP_PADDING
         apart for pin stubs / lane exits. Bonded host–support pairs skip this
         so supports can sit close to their host pin.

    When a support collides with its host, only the support moves. Sibling
    supports on the same host push each other; unrelated hits move whole
    host+supports clusters together.
    """
    bonded = bonded_pairs or set()
    support_of, hosts_supports, by_id = _support_host_maps(components)

    def _move_ids(ids: set[str], dx: float, dy: float) -> None:
        for mid in ids:
            m = by_id.get(mid)
            if m is not None:
                m["position"]["x"] += dx
                m["position"]["y"] += dy

    def _body_move_sets(id_i: str, id_j: str) -> tuple[set[str], set[str]]:
        """Who moves for a hard body collision between i and j."""
        # Support vs its own host → only the support moves
        if support_of.get(id_i) == id_j:
            return {id_i}, set()
        if support_of.get(id_j) == id_i:
            return set(), {id_j}
        # Sibling supports (same host) → just those two parts
        hi = support_of.get(id_i)
        hj = support_of.get(id_j)
        if hi and hi == hj:
            return {id_i}, {id_j}
        # Otherwise move whole clusters
        return (
            _cluster_member_ids(id_i, support_of, hosts_supports),
            _cluster_member_ids(id_j, support_of, hosts_supports),
        )

    for _ in range(max_iter):
        any_moved = False

        for i in range(len(components)):
            for j in range(i + 1, len(components)):
                ci = components[i]
                cj = components[j]
                id_i = str(ci.get("id", "")).strip()
                id_j = str(cj.get("id", "")).strip()
                if not id_i or not id_j:
                    continue

                # ── 1. Hard: component body boxes must not overlap ─────────
                ox, oy = _body_overlap_xy(ci, cj, gap=BODY_CLEARANCE)
                if ox > 0 and oy > 0:
                    move_a, move_b = _body_move_sets(id_i, id_j)
                    _push_apart(move_a, move_b, ci, cj, ox, oy, _move_ids)
                    any_moved = True
                    continue

                # ── 2. Soft: routing padding between unrelated clusters ────
                pair = frozenset({id_i, id_j})
                if pair in bonded:
                    continue

                cluster_i = _cluster_member_ids(id_i, support_of, hosts_supports)
                cluster_j = _cluster_member_ids(id_j, support_of, hosts_supports)
                if cluster_i & cluster_j:
                    continue

                ox, oy = _body_overlap_xy(ci, cj, gap=COMP_PADDING * 2)
                if ox <= 0 or oy <= 0:
                    continue

                _push_apart(cluster_i, cluster_j, ci, cj, ox, oy, _move_ids)
                any_moved = True

        if not any_moved:
            break


def _normalize_to_canvas(
    components: list,
    top_margin: float,
    left_margin: float = COMP_PADDING,
) -> None:
    """Shift all components so the layout clears the left and top margins.

    The canvas is dynamic (sized after this call), so there is no right/bottom
    limit to enforce here — the SVG simply grows to fit.
    """
    if not components:
        return
    min_x = min(c["position"]["x"] - COMP_PADDING for c in components)
    min_y = min(c["position"]["y"] - COMP_PADDING for c in components)
    shift_x = left_margin - min_x
    shift_y = top_margin - min_y
    if abs(shift_x) > 0.5 or abs(shift_y) > 0.5:
        for c in components:
            c["position"]["x"] += shift_x
            c["position"]["y"] += shift_y


def _pin_endpoint(comp: dict, pin: dict) -> tuple[float, float]:
    """Return (x, y) of the outer tip of a pin stub."""
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = _eff_h(comp)
    side = pin.get("side", "left")
    offset = float(pin.get("offset", 0.5))

    if side == "left":
        return (x - PIN_STUB, y + offset * h)
    elif side == "right":
        return (x + w + PIN_STUB, y + offset * h)
    elif side == "top":
        return (x + offset * w, y - PIN_STUB)
    else:  # bottom
        return (x + offset * w, y + h + PIN_STUB)


def _pin_lane_exit(comp: dict, pin: dict, pin_idx: int) -> tuple[float, float]:
    """
    Return the 'lane exit' point for a pin — the stub tip extended further out
    by (pin_idx + 1) * LANE_STEP pixels along the pin's axis.

    Each pin gets a unique lane distance so no two wires from the same component
    share the same breakout segment (comb routing).
    """
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = _eff_h(comp)
    side   = pin.get("side", "left")
    offset = float(pin.get("offset", 0.5))
    extra  = (pin_idx % MAX_LANES + 1) * LANE_STEP

    if side == "left":
        return (x - PIN_STUB - extra, y + offset * h)
    elif side == "right":
        return (x + w + PIN_STUB + extra, y + offset * h)
    elif side == "top":
        return (x + offset * w, y - PIN_STUB - extra)
    else:  # bottom
        return (x + offset * w, y + h + PIN_STUB + extra)


def _midpoint(
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> tuple[float, float]:
    return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)


def _preregister_stubs(
    registry: "_SegmentRegistry",
    components: list,
    pin_endpoints: dict,
    pin_lane_exits: dict,
) -> None:
    """Reserve tip→lane breakout segments so mid-routes do not collide with them."""
    for comp in components:
        for pin_idx, pin in enumerate(comp.get("pins", [])):
            key = (comp["id"], pin["number"])
            tip = pin_endpoints.get(key)
            lane = pin_lane_exits.get(key)
            if not tip or not lane:
                continue
            side = pin.get("side", "left")
            if side in {"left", "right"}:
                registry.register_h(tip[1], tip[0], lane[0])
            else:
                registry.register_v(tip[0], tip[1], lane[1])


def _pick_trunk_y(
    registry: "_SegmentRegistry",
    lanes: list[_PtF],
    obstacles: list[_Rect],
    max_tries: int = 30,
) -> float:
    """Choose a horizontal trunk Y clear of wires and foreign component bodies."""
    x_vals = [lx for lx, _ in lanes]
    x_min = min(x_vals) - WIRE_SPACING
    x_max = max(x_vals) + WIRE_SPACING
    y_centroid = sum(ly for _, ly in lanes) / len(lanes)

    offsets = [0]
    for k in range(1, max_tries):
        offsets += [k * WIRE_SPACING, -k * WIRE_SPACING]

    for off in offsets:
        ty = y_centroid + off
        trunk_pts = [(x_min, ty), (x_max, ty)]
        if not registry.path_ok(trunk_pts, obstacles):
            continue
        if any(
            not registry.path_ok([(lx, ly), (lx, ty)], obstacles)
            for lx, ly in lanes
        ):
            continue
        return ty
    return y_centroid


def _compute_trunk_geom(
    registry: "_SegmentRegistry",
    conn_pts: list[tuple[_PtF, _PtF]],
    obstacles: list[_Rect],
    max_tries: int = 30,
) -> dict:
    """
    Compute trunk geometry for a 3+ pin net and register its segments.
    Returns dict with trunk_y, x_min, x_max, junctions.
    """
    lanes = [lane for _, lane in conn_pts]
    x_vals = [lx for lx, _ in lanes]
    x_min = min(x_vals) - WIRE_SPACING
    x_max = max(x_vals) + WIRE_SPACING
    trunk_y = _pick_trunk_y(registry, lanes, obstacles, max_tries=max_tries)

    registry.register_h(trunk_y, x_min, x_max)
    junctions: list[_PtF] = []
    for lx, ly in lanes:
        registry.register_v(lx, ly, trunk_y)
        junctions.append((lx, trunk_y))

    return {
        "kind": "trunk",
        "trunk_y": trunk_y,
        "x_min": x_min,
        "x_max": x_max,
        "junctions": junctions,
        "conn_pts": conn_pts,
        "label_at": ((x_min + x_max) / 2, trunk_y),
    }


def _compute_two_pin_geom(
    registry: "_SegmentRegistry",
    tip1: _PtF,
    lane1: _PtF,
    tip2: _PtF,
    lane2: _PtF,
    obstacles: list[_Rect],
    max_tries: int = 12,
) -> dict:
    mid = registry.route(lane1, lane2, max_tries=max_tries, obstacles=obstacles)
    # tip→lane stubs are pre-registered; include them in the drawn polyline
    points = [tip1, lane1] + mid[1:-1] + [lane2, tip2]
    # Deduplicate consecutive equals
    cleaned: list[_PtF] = [points[0]]
    for p in points[1:]:
        if abs(p[0] - cleaned[-1][0]) > 0.5 or abs(p[1] - cleaned[-1][1]) > 0.5:
            cleaned.append(p)
    return {
        "kind": "two_pin",
        "points": cleaned,
        "label_at": _midpoint(lane1, lane2),
    }


def _route_one_signal(
    registry: "_SegmentRegistry",
    components: list,
    job: dict,
    max_tries: int,
) -> dict:
    attached = job["attached"]
    obstacles = _obstacles_for_net(components, attached)
    conn_pts = job["conn_pts"]
    if len(conn_pts) == 2:
        (tip1, lane1), (tip2, lane2) = conn_pts
        return _compute_two_pin_geom(
            registry, tip1, lane1, tip2, lane2, obstacles, max_tries=max_tries,
        )
    return _compute_trunk_geom(registry, conn_pts, obstacles, max_tries=max_tries + 10)


def _geom_still_conflicts(
    components: list,
    job: dict,
    geom: dict,
) -> bool:
    """True if this net's geometry still crosses a foreign body."""
    obstacles = _obstacles_for_net(components, job["attached"])
    if not obstacles:
        return False
    if geom["kind"] == "two_pin":
        return _path_hits_obstacles(geom["points"], obstacles)
    # trunk: check trunk + each vertical branch
    ty = geom["trunk_y"]
    if _path_hits_obstacles([(geom["x_min"], ty), (geom["x_max"], ty)], obstacles):
        return True
    for tip, lane in geom["conn_pts"]:
        lx, ly = lane
        if _path_hits_obstacles([tip, lane], obstacles):
            return True
        if _path_hits_obstacles([(lx, ly), (lx, ty)], obstacles):
            return True
    return False


def _multi_pass_route_signals(
    components: list,
    signal_jobs: list[dict],
    pin_endpoints: dict,
    pin_lane_exits: dict,
    passes: int = ROUTE_PASSES,
) -> list[dict]:
    """
    After component layout, route all signal nets ``passes`` times.

    Each pass rebuilds the segment registry, re-routes every net (avoiding
    foreign component bodies and other traces), and prefers nets that still
    conflict so later passes can clear them with wider search.
    """
    if not signal_jobs:
        return []

    n = len(signal_jobs)
    geoms: list[dict | None] = [None] * n
    # Indices that still need a better route (start with all)
    dirty = set(range(n))

    for pass_i in range(passes):
        registry = _SegmentRegistry()
        _preregister_stubs(registry, components, pin_endpoints, pin_lane_exits)

        # Wider offset search on later passes
        max_tries = 12 + pass_i * 6

        # Order: dirty nets first (resolve conflicts), then alternate direction
        clean = [i for i in range(n) if i not in dirty]
        dirty_list = sorted(dirty)
        if pass_i % 2 == 1:
            dirty_list = list(reversed(dirty_list))
            clean = list(reversed(clean))
        # On the last pass, also prefer longer nets first among dirty
        if pass_i == passes - 1:
            dirty_list = sorted(
                dirty_list,
                key=lambda i: -_net_span(signal_jobs[i]),
            )

        order = dirty_list + clean
        new_geoms: list[dict | None] = [None] * n
        for i in order:
            new_geoms[i] = _route_one_signal(
                registry, components, signal_jobs[i], max_tries=max_tries,
            )
        geoms = new_geoms  # type: ignore[assignment]

        # Mark nets that still pierce foreign bodies so the next pass
        # routes them first (with a wider search).
        dirty = {
            i for i in range(n)
            if geoms[i] is not None
            and _geom_still_conflicts(
                components, signal_jobs[i], geoms[i],  # type: ignore[arg-type]
            )
        }

    return [g for g in geoms]  # type: ignore[misc]


def _net_span(job: dict) -> float:
    """Rough geometric span — longer nets get priority on the final pass."""
    xs = [lane[0] for _, lane in job["conn_pts"]]
    ys = [lane[1] for _, lane in job["conn_pts"]]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _draw_routed_net(
    dwg: svgwrite.Drawing,
    wire_group,
    net_label_group,
    job: dict,
    geom: dict,
) -> None:
    """Draw a previously computed signal-net geometry."""
    stroke = job["stroke"]
    name = job["name"]

    if geom["kind"] == "two_pin":
        wire_group.add(dwg.polyline(
            points=geom["points"],
            stroke=stroke, stroke_width=WIRE_W, fill="none",
            stroke_linejoin="round", stroke_linecap="round",
        ))
        _draw_net_label(dwg, net_label_group, name, *geom["label_at"])
        return

    # Trunk
    ty = geom["trunk_y"]
    wire_group.add(dwg.line(
        start=(geom["x_min"], ty), end=(geom["x_max"], ty),
        stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
    ))
    for tip, lane in geom["conn_pts"]:
        lx, ly = lane
        if abs(tip[0] - lx) > 1 or abs(tip[1] - ly) > 1:
            wire_group.add(dwg.line(
                start=tip, end=lane,
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
        if abs(ly - ty) > 1:
            wire_group.add(dwg.line(
                start=(lx, ly), end=(lx, ty),
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
        wire_group.add(dwg.circle(center=(lx, ty), r=3, fill=stroke))
    _draw_net_label(dwg, net_label_group, name, *geom["label_at"])


def _draw_component(dwg: svgwrite.Drawing, group, comp: dict) -> None:
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = _eff_h(comp)
    cid       = comp.get("id", "?")
    name      = comp.get("name", "") or comp.get("type", "")
    value     = comp.get("value", "")
    comp_type = comp.get("type", "").lower()
    compact   = _is_common_part(comp)

    # ── Body ──────────────────────────────────────────────────────────────
    group.add(dwg.rect(
        insert=(x, y), size=(w, h),
        fill=COMP_FILL, fill_opacity=0.85,
        stroke=COMP_STROKE, stroke_width=COMP_STROKE_W,
        rx=3, ry=3,
    ))

    cx = x + w / 2          # horizontal centre of box

    # ── Zone layout (3 equal horizontal bands) ────────────────────────────
    # Compact 2-pin parts use smaller type so half-height boxes still fit.
    zone = h / 3
    label_fs = 9 if compact else LABEL_FONT
    name_fs = 8 if compact else FONT_SIZE
    value_fs = 7 if compact else FONT_SIZE - 1

    # Zone 1 — Ref Des (top band, centred)
    group.add(dwg.text(
        cid,
        insert=(cx, y + zone * 0.55),
        font_size=label_fs,
        font_family="monospace",
        font_weight="bold",
        fill="#222",
        text_anchor="middle",
        dominant_baseline="middle",
    ))

    # Zone 2 — KiCad-style schematic symbol (middle band)
    _draw_comp_symbol(dwg, group, comp_type, cx, y + zone * 1.5, w, zone)

    # Zone 3 — Name then value (bottom band, centred)
    # Compact: prefer value alone when both exist (value is the useful label).
    if compact and value:
        bottom_lines = [value]
        bottom_sizes = [value_fs]
        bottom_styles = ["italic"]
        bottom_fills = ["#666"]
    else:
        bottom_lines = [name] + ([value] if value else [])
        bottom_sizes = [name_fs] + ([value_fs] if value else [])
        bottom_styles = ["normal"] + (["italic"] if value else [])
        bottom_fills = ["#444"] + (["#666"] if value else [])

    line_h = (name_fs if compact else FONT_SIZE) + (1 if compact else 2)
    n_lines = len(bottom_lines)
    y_start = y + zone * 2 + (zone - n_lines * line_h) / 2 + name_fs

    for i, (text, fs, style, fill) in enumerate(
        zip(bottom_lines, bottom_sizes, bottom_styles, bottom_fills)
    ):
        group.add(dwg.text(
            text,
            insert=(cx, y_start + i * line_h),
            font_size=fs,
            font_family="monospace",
            font_style=style,
            fill=fill,
            text_anchor="middle",
        ))

    # ── Divider lines between zones ────────────────────────────────────────
    for frac in (1/3, 2/3):
        group.add(dwg.line(
            start=(x + 2, y + h * frac), end=(x + w - 2, y + h * frac),
            stroke=COMP_STROKE, stroke_width=0.4, stroke_dasharray="3,3",
            opacity="0.4",
        ))

    # ── Pin stubs ──────────────────────────────────────────────────────────
    for pin in comp.get("pins", []):
        _draw_pin_stub(dwg, group, comp, pin)


# ---------------------------------------------------------------------------
# Component symbol graphics (drawn inside Zone 2)
# ---------------------------------------------------------------------------

def _draw_comp_symbol(
    dwg: svgwrite.Drawing, group, comp_type: str,
    cx: float, cy: float, box_w: float, zone_h: float,
) -> None:
    """Dispatch to a type-specific schematic symbol centred at (cx, cy)."""
    s = min(box_w * 0.35, zone_h * 0.65, 28)   # symbol scale
    t = comp_type.lower()

    if "resistor" in t:
        _sym_resistor(dwg, group, cx, cy, s)
    elif "capacitor" in t or t in {"cap", "bypass"}:
        _sym_capacitor(dwg, group, cx, cy, s)
    elif "inductor" in t or "coil" in t or "ferrite" in t:
        _sym_inductor(dwg, group, cx, cy, s)
    elif any(x in t for x in ["led", "diode", "schottky", "zener", "tvs"]):
        _sym_diode(dwg, group, cx, cy, s)
    elif any(x in t for x in ["transistor", "mosfet", "bjt", "npn", "pnp", "fet"]):
        _sym_transistor(dwg, group, cx, cy, s)
    elif any(x in t for x in ["crystal", "xtal", "resonator"]):
        _sym_crystal(dwg, group, cx, cy, s)
    elif any(x in t for x in ["connector", "header", "jack", "plug", "socket", "usb"]):
        _sym_connector(dwg, group, cx, cy, s)
    else:
        _sym_ic(dwg, group, cx, cy, s)


def _sym_resistor(dwg, group, cx, cy, s):
    """IEC rectangle resistor symbol."""
    rw, rh = s * 1.6, s * 0.7
    group.add(dwg.rect(
        insert=(cx - rw / 2, cy - rh / 2), size=(rw, rh),
        fill="none", stroke=COMP_STROKE, stroke_width=1.5,
    ))


def _sym_capacitor(dwg, group, cx, cy, s):
    """Two parallel plates."""
    hw  = s * 0.9
    gap = max(5, s * 0.4)
    for dy in (-gap / 2, gap / 2):
        group.add(dwg.line(
            start=(cx - hw, cy + dy), end=(cx + hw, cy + dy),
            stroke=COMP_STROKE, stroke_width=2, stroke_linecap="round",
        ))


def _sym_inductor(dwg, group, cx, cy, s):
    """Three arcs."""
    r  = s * 0.35
    n  = 3
    x0 = cx - r * n
    for i in range(n):
        ax = x0 + (2 * i + 1) * r
        group.add(dwg.path(
            d=f"M {ax-r:.1f} {cy:.1f} A {r:.1f} {r:.1f} 0 0 1 {ax+r:.1f} {cy:.1f}",
            fill="none", stroke=COMP_STROKE, stroke_width=1.5,
        ))


def _sym_diode(dwg, group, cx, cy, s):
    """Triangle + cathode bar."""
    h = s * 0.9
    group.add(dwg.polygon(
        points=[(cx - s, cy - h / 2), (cx - s, cy + h / 2), (cx + s * 0.5, cy)],
        fill=COMP_STROKE, stroke=COMP_STROKE, stroke_width=1,
    ))
    group.add(dwg.line(
        start=(cx + s * 0.5, cy - h / 2), end=(cx + s * 0.5, cy + h / 2),
        stroke=COMP_STROKE, stroke_width=2,
    ))


def _sym_transistor(dwg, group, cx, cy, s):
    """Simplified transistor (collector/base/emitter lines)."""
    group.add(dwg.line(         # vertical base
        start=(cx - s * 0.3, cy - s), end=(cx - s * 0.3, cy + s),
        stroke=COMP_STROKE, stroke_width=2,
    ))
    group.add(dwg.line(         # base stub
        start=(cx - s, cy), end=(cx - s * 0.3, cy),
        stroke=COMP_STROKE, stroke_width=1.5,
    ))
    group.add(dwg.line(         # collector (diagonal up)
        start=(cx - s * 0.3, cy - s * 0.5), end=(cx + s, cy - s),
        stroke=COMP_STROKE, stroke_width=1.5,
    ))
    group.add(dwg.line(         # emitter (diagonal down with arrow)
        start=(cx - s * 0.3, cy + s * 0.5), end=(cx + s, cy + s),
        stroke=COMP_STROKE, stroke_width=1.5,
    ))


def _sym_crystal(dwg, group, cx, cy, s):
    """Rectangle with lines extending top and bottom."""
    rw, rh = s * 0.5, s * 1.2
    group.add(dwg.rect(
        insert=(cx - rw / 2, cy - rh / 2), size=(rw, rh),
        fill=COMP_FILL, stroke=COMP_STROKE, stroke_width=1.5,
    ))
    for dy in (-rh / 2, rh / 2):
        group.add(dwg.line(
            start=(cx - s * 0.8, cy + dy), end=(cx + s * 0.8, cy + dy),
            stroke=COMP_STROKE, stroke_width=1.5,
        ))


def _sym_connector(dwg, group, cx, cy, s):
    """Three horizontal contact lines."""
    spacing = s * 0.55
    hw = s * 0.75
    for i in range(3):
        yy = cy + (i - 1) * spacing
        group.add(dwg.line(
            start=(cx - hw, yy), end=(cx + hw, yy),
            stroke=COMP_STROKE, stroke_width=1.5, stroke_linecap="round",
        ))


def _sym_ic(dwg, group, cx, cy, s):
    """Generic IC: dashed inner rectangle."""
    group.add(dwg.rect(
        insert=(cx - s, cy - s * 0.7), size=(s * 2, s * 1.4),
        fill="none", stroke=COMP_STROKE, stroke_width=1,
        stroke_dasharray="4,3",
    ))


def _draw_pin_stub(dwg: svgwrite.Drawing, group, comp: dict, pin: dict) -> None:
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = _eff_h(comp)
    side = pin.get("side", "left")
    offset = float(pin.get("offset", 0.5))
    pin_name = pin.get("name", "")
    pin_num = str(pin.get("number", ""))

    # Body edge point (inside)
    if side == "left":
        body_x, body_y = x, y + offset * h
        tip_x, tip_y = x - PIN_STUB, body_y
        label_x = tip_x - 2
        label_anchor = "end"
        num_x, num_y = body_x + 2, body_y - 1
    elif side == "right":
        body_x, body_y = x + w, y + offset * h
        tip_x, tip_y = x + w + PIN_STUB, body_y
        label_x = tip_x + 2
        label_anchor = "start"
        num_x, num_y = body_x - 2, body_y - 1
    elif side == "top":
        body_x, body_y = x + offset * w, y
        tip_x, tip_y = body_x, y - PIN_STUB
        label_x, label_anchor = body_x, "middle"
        num_x, num_y = body_x, body_y + PIN_FONT + 1
    else:  # bottom
        body_x, body_y = x + offset * w, y + h
        tip_x, tip_y = body_x, y + h + PIN_STUB
        label_x, label_anchor = body_x, "middle"
        num_x, num_y = body_x, body_y - 2

    # Stub line
    group.add(dwg.line(
        start=(body_x, body_y),
        end=(tip_x, tip_y),
        stroke="#667",
        stroke_width=1,
    ))

    # Pin name label (outside)
    if pin_name:
        kwargs = {
            "insert": (label_x, tip_y + PIN_FONT / 3),
            "font_size": PIN_FONT,
            "font_family": "monospace",
            "fill": "#445",
            "text_anchor": label_anchor,
        }
        if side in {"top", "bottom"}:
            kwargs["transform"] = f"rotate(-90,{label_x},{tip_y})"
            kwargs["insert"] = (label_x, tip_y - 2)
        group.add(dwg.text(pin_name, **kwargs))

    # Pin number (inside body edge, tiny)
    group.add(dwg.text(
        pin_num,
        insert=(num_x, num_y),
        font_size=PIN_FONT - 1,
        font_family="monospace",
        fill="#999",
        text_anchor="middle" if side in {"top", "bottom"} else "start",
    ))


def _draw_net_label(
    dwg: svgwrite.Drawing,
    group,
    name: str,
    x: float,
    y: float,
) -> None:
    """Small flag label on a wire."""
    pad = 3
    tw = len(name) * 6 + pad * 2
    th = FONT_SIZE + pad * 2
    group.add(dwg.rect(
        insert=(x - tw / 2, y - th / 2),
        size=(tw, th),
        fill=NET_LABEL_FILL,
        stroke=NET_LABEL_STROKE,
        stroke_width=0.5,
        rx=2,
    ))
    group.add(dwg.text(
        name,
        insert=(x, y + FONT_SIZE / 3),
        font_size=FONT_SIZE - 1,
        font_family="monospace",
        fill="#333",
        text_anchor="middle",
    ))


def _draw_power_bus(
    dwg: svgwrite.Drawing,
    wire_group,
    net_label_group,
    registry: "_SegmentRegistry",
    conn_pts: list,
    name: str,
    rail_y: float,
    stroke: str,
) -> None:
    """Horizontal power bus at rail_y with vertical stubs from each pin's lane_exit."""
    if not conn_pts:
        return
    lane_xs = [lane[0] for _, lane in conn_pts]
    bus_x1 = max(10.0, min(lane_xs) - 30)
    bus_x2 = min(float(CANVAS_W - 10), max(lane_xs) + 30)

    # Bus line
    wire_group.add(dwg.line(
        start=(bus_x1, rail_y), end=(bus_x2, rail_y),
        stroke=stroke, stroke_width=2.5, stroke_linecap="round",
    ))
    registry.register_h(rail_y, bus_x1, bus_x2)

    for tip, lane in conn_pts:
        lx, ly = lane
        # Comb breakout: tip → lane_exit
        if abs(tip[0] - lx) > 1 or abs(tip[1] - ly) > 1:
            wire_group.add(dwg.line(
                start=tip, end=lane,
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
        # Vertical stub: lane_exit → bus
        if abs(ly - rail_y) > 1:
            wire_group.add(dwg.line(
                start=(lx, ly), end=(lx, rail_y),
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
            registry.register_v(lx, ly, rail_y)
        # Junction dot where stub meets bus
        wire_group.add(dwg.circle(center=(lx, rail_y), r=3, fill=stroke))

    # Net label centred on bus
    mid_x = (bus_x1 + bus_x2) / 2
    _draw_net_label(dwg, net_label_group, name, mid_x, rail_y)


def _draw_power_symbol(
    dwg: svgwrite.Drawing,
    group,
    net_name: str,
    x: float,
    y: float,
    side: str = "bottom",
) -> None:
    """Draw VCC (arrow) or GND (flat bars) power symbol at the pin tip.

    The symbol extends outward from the pin along ``side``.
    """
    is_gnd = _is_gnd_net(net_name)
    stroke = _gnd_stroke(net_name) if is_gnd else _power_stroke(net_name)

    if is_gnd:
        # Three bars decreasing in width (IEEE GND), oriented outward from pin
        bar_widths = [16, 10, 4]
        step = 4
        for i, bar_w in enumerate(bar_widths):
            half = bar_w / 2
            if side == "top":
                yy = y - i * step
                pts = ((x - half, yy), (x + half, yy))
            elif side == "left":
                xx = x - i * step
                pts = ((xx, y - half), (xx, y + half))
            elif side == "right":
                xx = x + i * step
                pts = ((xx, y - half), (xx, y + half))
            else:  # bottom (default)
                yy = y + i * step
                pts = ((x - half, yy), (x + half, yy))
            group.add(dwg.line(
                start=pts[0], end=pts[1],
                stroke=stroke,
                stroke_width=1.5,
                stroke_linecap="round",
            ))
        label_off = {
            "top": (x, y - 20),
            "left": (x - 22, y + 3),
            "right": (x + 22, y + 3),
            "bottom": (x, y + 20),
        }.get(side, (x, y + 20))
        group.add(dwg.text(
            net_name,
            insert=label_off,
            font_size=PIN_FONT,
            font_family="monospace",
            fill=stroke,
            text_anchor="middle",
        ))
    else:
        # Arrow pointing outward + net name label
        arrow_h = 16
        arrow_w = 8
        if side == "bottom":
            tip = (x, y + arrow_h + 8)
            base_y = y + arrow_h
            group.add(dwg.line(start=(x, y), end=(x, base_y),
                               stroke=stroke, stroke_width=1.5))
            group.add(dwg.polygon(
                points=[tip, (x - arrow_w / 2, base_y), (x + arrow_w / 2, base_y)],
                fill=stroke,
            ))
            label_at = (x, y + arrow_h + 20)
        elif side == "left":
            tip = (x - arrow_h - 8, y)
            base_x = x - arrow_h
            group.add(dwg.line(start=(x, y), end=(base_x, y),
                               stroke=stroke, stroke_width=1.5))
            group.add(dwg.polygon(
                points=[tip, (base_x, y - arrow_w / 2), (base_x, y + arrow_w / 2)],
                fill=stroke,
            ))
            label_at = (x - arrow_h - 14, y - 10)
        elif side == "right":
            tip = (x + arrow_h + 8, y)
            base_x = x + arrow_h
            group.add(dwg.line(start=(x, y), end=(base_x, y),
                               stroke=stroke, stroke_width=1.5))
            group.add(dwg.polygon(
                points=[tip, (base_x, y - arrow_w / 2), (base_x, y + arrow_w / 2)],
                fill=stroke,
            ))
            label_at = (x + arrow_h + 14, y - 10)
        else:  # top (default for VCC)
            tip = (x, y - arrow_h - 8)
            base_y = y - arrow_h
            group.add(dwg.line(start=(x, y), end=(x, base_y),
                               stroke=stroke, stroke_width=1.5))
            group.add(dwg.polygon(
                points=[tip, (x - arrow_w / 2, base_y), (x + arrow_w / 2, base_y)],
                fill=stroke,
            ))
            label_at = (x, y - arrow_h - 12)
        group.add(dwg.text(
            net_name,
            insert=label_at,
            font_size=PIN_FONT,
            font_family="monospace",
            fill=stroke,
            text_anchor="middle",
        ))
