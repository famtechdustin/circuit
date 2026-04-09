"""
svg_generator.py — Generate a 2D schematic SVG from a circuit JSON dict.

Components are drawn as labeled rectangles with pin stubs on the specified side.
Nets are drawn as right-angle (Manhattan) wires between pin endpoints.
Power rails (VCC, GND, etc.) receive standard schematic symbols.
"""

import copy
import io
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
WIRE_STROKE = "#1a4fa0"
WIRE_W = 1.5
POWER_STROKE = "#cc2200"
GND_STROKE = "#222222"
NET_LABEL_FILL = "#ffffff"
NET_LABEL_STROKE = "#888888"
GRID = 10
WIRE_SPACING = 12   # px to shift mid-bend when a conflict is detected
SEG_TOL = 3.0       # px tolerance for "same axis" overlap check
LANE_STEP = 14      # px per lane index for the dedicated breakout
MAX_LANES = 6       # cap: lane distance cycles after this many pins (max extra = 84 px)
MIN_COMP_H = 90     # minimum rendered component height (3 text zones)
# Clearance added around each component body when checking for overlaps.
# Must cover pin stubs (PIN_STUB) + max lane exit (MAX_LANES * LANE_STEP) + routing room.
COMP_PADDING = PIN_STUB + MAX_LANES * LANE_STEP + 20   # ≈ 118 px
RAIL_TOP_START = 35          # Y of topmost VCC-type rail
RAIL_BOT_START = CANVAS_H - 35  # Y of bottommost GND-type rail
RAIL_SPACING = 22            # px between stacked rails of same polarity

_GND_NAMES = {"GND", "AGND", "DGND", "PGND", "0V"}


def _is_gnd_net(name: str) -> bool:
    return name.upper() in _GND_NAMES


def _eff_h(comp: dict) -> float:
    """Effective component height — enforces MIN_COMP_H regardless of JSON value."""
    return max(float(comp["bounding_box"]["height"]), MIN_COMP_H)


# ---------------------------------------------------------------------------
# Segment registry — tracks axis-aligned wire segments to prevent overlap
# ---------------------------------------------------------------------------

class _SegmentRegistry:
    """
    Keeps a list of horizontal and vertical wire segments already drawn.
    Used to detect overlapping routes and choose an offset bend point.
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

    def route(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        max_tries: int = 9,
    ) -> list[tuple[float, float]]:
        """
        Return an L-shaped waypoint list from p1 to p2, shifting the bend
        point horizontally until no registered segment conflicts are found.
        Registers the chosen segments before returning.
        """
        x1, y1 = p1
        x2, y2 = p2

        # Pure horizontal
        if abs(y1 - y2) < self.tol:
            self.register_h(y1, x1, x2)
            return [p1, p2]

        # Pure vertical
        if abs(x1 - x2) < self.tol:
            self.register_v(x1, y1, y2)
            return [p1, p2]

        # General: try offsets of the mid-x bend point
        base_mid = (x1 + x2) / 2
        offsets = [0]
        for k in range(1, max_tries):
            offsets.append( k * WIRE_SPACING)
            offsets.append(-k * WIRE_SPACING)

        for off in offsets:
            mx = base_mid + off
            if (not self._h_conflict(y1, x1, mx) and
                    not self._v_conflict(mx, y1, y2) and
                    not self._h_conflict(y2, mx, x2)):
                self.register_h(y1, x1, mx)
                self.register_v(mx, y1, y2)
                self.register_h(y2, mx, x2)
                return [p1, (mx, y1), (mx, y2), p2]

        # Fallback: use base mid even if conflicted (draws on top — rare)
        self.register_h(y1, x1, base_mid)
        self.register_v(base_mid, y1, y2)
        self.register_h(y2, base_mid, x2)
        return [p1, (base_mid, y1), (base_mid, y2), p2]

    # expose tol as property for the pure-H/V checks above
    @property
    def tol(self) -> float:
        return self._tol


def generate_svg(circuit: dict) -> str:
    """
    Convert a circuit dict (matching the JSON schema) to an SVG string.
    Returns the complete SVG as a str.
    """
    dwg = svgwrite.Drawing(
        size=(f"{CANVAS_W}px", f"{CANVAS_H}px"),
        profile="full",
    )
    dwg.viewbox(0, 0, CANVAS_W, CANVAS_H)

    # Background
    dwg.add(dwg.rect(insert=(0, 0), size=(CANVAS_W, CANVAS_H), fill="#fafafa"))

    # Grid dots (subtle)
    grid_group = dwg.add(dwg.g(id="grid", opacity="0.15"))
    for gx in range(0, CANVAS_W, GRID * 5):
        for gy in range(0, CANVAS_H, GRID * 5):
            grid_group.add(dwg.circle(center=(gx, gy), r=0.8, fill="#999"))

    # Deep-copy so the resolver can adjust positions without mutating circuit.json
    components = copy.deepcopy(circuit.get("components", []))
    _resolve_overlaps(components)

    nets        = circuit.get("nets", [])
    power_rails = set(circuit.get("power_rails", []))

    # Classify power rails and assign bus Y positions
    vcc_nets = sorted(n for n in power_rails if not _is_gnd_net(n))
    gnd_nets = sorted(n for n in power_rails if _is_gnd_net(n))
    rail_y: dict[str, float] = {}
    for i, n in enumerate(vcc_nets):
        rail_y[n] = RAIL_TOP_START + i * RAIL_SPACING
    for i, n in enumerate(gnd_nets):
        rail_y[n] = RAIL_BOT_START - i * RAIL_SPACING

    # Reserve space below VCC rails before components start
    top_margin = RAIL_TOP_START + max(1, len(vcc_nets)) * RAIL_SPACING + 50
    _normalize_to_canvas(components, top_margin=top_margin)

    # Build pin lookups:
    #   pin_endpoints  — stub tip (the visible pin end)
    #   pin_lane_exits — further out by (pin_idx+1)*LANE_STEP, one unique lane per pin
    pin_endpoints:   dict[tuple, tuple[float, float]] = {}
    pin_lane_exits:  dict[tuple, tuple[float, float]] = {}
    for comp in components:
        for pin_idx, pin in enumerate(comp.get("pins", [])):
            key = (comp["id"], pin["number"])
            pin_endpoints[key]  = _pin_endpoint(comp, pin)
            pin_lane_exits[key] = _pin_lane_exit(comp, pin, pin_idx)

    # Pre-register all stub→lane segments so the mid-router avoids them
    registry = _SegmentRegistry()
    for comp in components:
        for pin_idx, pin in enumerate(comp.get("pins", [])):
            key  = (comp["id"], pin["number"])
            tip  = pin_endpoints[key]
            lane = pin_lane_exits[key]
            side = pin.get("side", "left")
            if side in {"left", "right"}:
                registry.register_h(tip[1], tip[0], lane[0])
            else:
                registry.register_v(tip[0], tip[1], lane[1])

    wire_group      = dwg.add(dwg.g(id="wires"))
    net_label_group = dwg.add(dwg.g(id="net-labels"))

    for net in nets:
        name        = net.get("name", "")
        connections = net.get("connections", [])
        is_power    = name in power_rails

        # Collect (tip, lane_exit) pairs for each connection
        conn_pts: list[tuple[tuple, tuple]] = []
        for conn in connections:
            key = (conn.get("component_id", ""), conn.get("pin_number", -1))
            tip  = pin_endpoints.get(key)
            lane = pin_lane_exits.get(key, tip)
            if tip:
                conn_pts.append((tip, lane))

        stroke = (
            POWER_STROKE if is_power and name.upper() not in {"GND", "AGND", "DGND"}
            else GND_STROKE if name.upper() in {"GND", "AGND", "DGND"}
            else WIRE_STROKE
        )

        # Power rails: horizontal bus + vertical stubs
        if is_power:
            ry = rail_y.get(name)
            if ry is not None and conn_pts:
                _draw_power_bus(dwg, wire_group, net_label_group,
                                registry, conn_pts, name, ry, stroke)
            else:
                for tip, _ in conn_pts:
                    _draw_power_symbol(dwg, wire_group, name, tip[0], tip[1])
            continue

        if len(conn_pts) < 2:
            continue

        if len(conn_pts) == 2:
            # Two endpoints: single L-shaped route
            tip1, lane1 = conn_pts[0]
            tip2, lane2 = conn_pts[1]
            mid = registry.route(lane1, lane2)
            wire_group.add(dwg.polyline(
                points=[tip1] + mid + [tip2],
                stroke=stroke, stroke_width=WIRE_W, fill="none",
                stroke_linejoin="round", stroke_linecap="round",
            ))
            _draw_net_label(dwg, net_label_group, name,
                            *_midpoint(lane1, lane2))
        else:
            # Three or more endpoints: horizontal trunk with vertical stubs
            _draw_trunk_net(dwg, wire_group, net_label_group,
                            registry, conn_pts, stroke, name)

    # Draw components on top of wires
    comp_group = dwg.add(dwg.g(id="components"))
    for comp in components:
        _draw_component(dwg, comp_group, comp)

    # Title block
    title = circuit.get("metadata", {}).get("title", "Untitled Circuit")
    revision = circuit.get("metadata", {}).get("revision", 1)
    dwg.add(dwg.text(
        f"{title}  Rev.{revision}",
        insert=(10, CANVAS_H - 10),
        font_size=12,
        font_family="monospace",
        fill="#666",
    ))

    buf = io.StringIO()
    dwg.write(buf)
    return buf.getvalue()


def _resolve_overlaps(components: list, max_iter: int = 60) -> None:
    """
    Iteratively push overlapping component boxes apart (in-place).

    Each component is treated as a rectangle expanded by COMP_PADDING on all
    sides to account for pin stubs, lane exits, and routing clearance.
    On each pass all pairs are checked; overlapping pairs are nudged apart
    in whichever axis requires the smaller displacement.  Repeats until no
    pair overlaps or max_iter is reached.
    """
    for _ in range(max_iter):
        any_moved = False

        for i in range(len(components)):
            for j in range(i + 1, len(components)):
                ci = components[i]
                cj = components[j]

                xi = ci["position"]["x"];  yi = ci["position"]["y"]
                wi = ci["bounding_box"]["width"];  hi = _eff_h(ci)
                xj = cj["position"]["x"];  yj = cj["position"]["y"]
                wj = cj["bounding_box"]["width"];  hj = _eff_h(cj)

                # Expanded rects
                ax1, ay1 = xi - COMP_PADDING,        yi - COMP_PADDING
                ax2, ay2 = xi + wi + COMP_PADDING,   yi + hi + COMP_PADDING
                bx1, by1 = xj - COMP_PADDING,        yj - COMP_PADDING
                bx2, by2 = xj + wj + COMP_PADDING,   yj + hj + COMP_PADDING

                ox = min(ax2, bx2) - max(ax1, bx1)
                oy = min(ay2, by2) - max(ay1, by1)

                if ox <= 0 or oy <= 0:
                    continue   # already clear

                # Push in the direction of smaller overlap to minimise movement
                if ox <= oy:
                    half = ox / 2 + 1
                    if (xi + wi / 2) <= (xj + wj / 2):
                        ci["position"]["x"] -= half
                        cj["position"]["x"] += half
                    else:
                        ci["position"]["x"] += half
                        cj["position"]["x"] -= half
                else:
                    half = oy / 2 + 1
                    if (yi + hi / 2) <= (yj + hj / 2):
                        ci["position"]["y"] -= half
                        cj["position"]["y"] += half
                    else:
                        ci["position"]["y"] += half
                        cj["position"]["y"] -= half

                any_moved = True

        if not any_moved:
            break


def _normalize_to_canvas(
    components: list,
    top_margin: float,
    left_margin: float = COMP_PADDING,
) -> None:
    """Translate all components so none escape the top or left canvas boundary."""
    if not components:
        return
    min_x = min(c["position"]["x"] - COMP_PADDING for c in components)
    min_y = min(c["position"]["y"] - COMP_PADDING for c in components)
    shift_x = max(0.0, left_margin - min_x)
    shift_y = max(0.0, top_margin - min_y)
    if shift_x > 0 or shift_y > 0:
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


def _draw_trunk_net(
    dwg: svgwrite.Drawing,
    wire_group,
    net_label_group,
    registry: "_SegmentRegistry",
    conn_pts: list,
    stroke: str,
    name: str,
) -> None:
    """
    Route a net with 3+ connections via a horizontal trunk line.

    Each pin gets:  tip → lane_exit → vertical stub → horizontal trunk
    The trunk is a single horizontal line at a conflict-free Y, connecting
    the leftmost and rightmost vertical stub X positions.
    This avoids the chain-routing problem (wires doubling back mid-route)
    and keeps every pin's trace in its own lane.
    """
    lanes = [lane for _, lane in conn_pts]
    x_vals = [lx for lx, _ in lanes]
    x_min = min(x_vals) - WIRE_SPACING
    x_max = max(x_vals) + WIRE_SPACING

    # Find a conflict-free horizontal trunk Y near the centroid of lane exits
    y_centroid = sum(ly for _, ly in lanes) / len(lanes)
    trunk_y = y_centroid
    offsets = [0]
    for k in range(1, 25):
        offsets += [k * WIRE_SPACING, -k * WIRE_SPACING]

    for off in offsets:
        ty = y_centroid + off
        h_clear = not registry._h_conflict(ty, x_min, x_max)
        v_clear = all(not registry._v_conflict(lx, ly, ty) for lx, ly in lanes)
        if h_clear and v_clear:
            trunk_y = ty
            break

    # Register trunk + all vertical branches
    registry.register_h(trunk_y, x_min, x_max)
    for lx, ly in lanes:
        registry.register_v(lx, ly, trunk_y)

    # Draw the trunk
    wire_group.add(dwg.line(
        start=(x_min, trunk_y), end=(x_max, trunk_y),
        stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
    ))

    # Draw each connection: tip → lane → vertical stub to trunk
    for tip, lane in conn_pts:
        lx, ly = lane
        # Stub extension: tip → lane_exit (unique comb tooth)
        if abs(tip[0] - lx) > 1 or abs(tip[1] - ly) > 1:
            wire_group.add(dwg.line(
                start=tip, end=lane,
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
        # Vertical branch: lane_exit → trunk
        if abs(ly - trunk_y) > 1:
            wire_group.add(dwg.line(
                start=(lx, ly), end=(lx, trunk_y),
                stroke=stroke, stroke_width=WIRE_W, stroke_linecap="round",
            ))
        # Junction dot where stub meets trunk
        wire_group.add(dwg.circle(center=(lx, trunk_y), r=3, fill=stroke))

    # Net label centred on the trunk
    mid_x = (x_min + x_max) / 2
    _draw_net_label(dwg, net_label_group, name, mid_x, trunk_y)


def _draw_component(dwg: svgwrite.Drawing, group, comp: dict) -> None:
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = _eff_h(comp)
    cid       = comp.get("id", "?")
    name      = comp.get("name", "") or comp.get("type", "")
    value     = comp.get("value", "")
    comp_type = comp.get("type", "").lower()

    # ── Body ──────────────────────────────────────────────────────────────
    group.add(dwg.rect(
        insert=(x, y), size=(w, h),
        fill=COMP_FILL, stroke=COMP_STROKE, stroke_width=COMP_STROKE_W,
        rx=3, ry=3,
    ))

    cx = x + w / 2          # horizontal centre of box

    # ── Zone layout (3 equal horizontal bands) ────────────────────────────
    zone = h / 3

    # Zone 1 — Ref Des (top band, centred)
    group.add(dwg.text(
        cid,
        insert=(cx, y + zone * 0.55),
        font_size=LABEL_FONT,
        font_family="monospace",
        font_weight="bold",
        fill="#222",
        text_anchor="middle",
        dominant_baseline="middle",
    ))

    # Zone 2 — KiCad-style schematic symbol (middle band)
    _draw_comp_symbol(dwg, group, comp_type, cx, y + zone * 1.5, w, zone)

    # Zone 3 — Name then value (bottom band, centred)
    line_h = FONT_SIZE + 2
    n_lines = 2 if value else 1
    y_start = y + zone * 2 + (zone - n_lines * line_h) / 2 + FONT_SIZE

    group.add(dwg.text(
        name,
        insert=(cx, y_start),
        font_size=FONT_SIZE,
        font_family="monospace",
        fill="#444",
        text_anchor="middle",
    ))
    if value:
        group.add(dwg.text(
            value,
            insert=(cx, y_start + line_h),
            font_size=FONT_SIZE - 1,
            font_family="monospace",
            font_style="italic",
            fill="#666",
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
) -> None:
    """Draw VCC (arrow up) or GND (flat bars) power symbol at the pin tip."""
    name_upper = net_name.upper()
    is_gnd = name_upper in {"GND", "AGND", "DGND", "PGND", "0V"}

    if is_gnd:
        # Three horizontal bars decreasing in width (IEEE GND symbol)
        for i, bar_w in enumerate([16, 10, 4]):
            yy = y + i * 4
            group.add(dwg.line(
                start=(x - bar_w / 2, yy),
                end=(x + bar_w / 2, yy),
                stroke=GND_STROKE,
                stroke_width=1.5,
                stroke_linecap="round",
            ))
        group.add(dwg.text(
            net_name,
            insert=(x, y + 20),
            font_size=PIN_FONT,
            font_family="monospace",
            fill=GND_STROKE,
            text_anchor="middle",
        ))
    else:
        # Arrow pointing up + net name label
        arrow_h = 16
        arrow_w = 8
        group.add(dwg.line(
            start=(x, y),
            end=(x, y - arrow_h),
            stroke=POWER_STROKE,
            stroke_width=1.5,
        ))
        group.add(dwg.polygon(
            points=[(x, y - arrow_h - 8), (x - arrow_w / 2, y - arrow_h), (x + arrow_w / 2, y - arrow_h)],
            fill=POWER_STROKE,
        ))
        group.add(dwg.text(
            net_name,
            insert=(x, y - arrow_h - 12),
            font_size=PIN_FONT,
            font_family="monospace",
            fill=POWER_STROKE,
            text_anchor="middle",
        ))
