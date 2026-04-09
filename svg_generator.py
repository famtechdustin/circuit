"""
svg_generator.py — Generate a 2D schematic SVG from a circuit JSON dict.

Components are drawn as labeled rectangles with pin stubs on the specified side.
Nets are drawn as right-angle (Manhattan) wires between pin endpoints.
Power rails (VCC, GND, etc.) receive standard schematic symbols.
"""

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

    components = circuit.get("components", [])
    nets = circuit.get("nets", [])
    power_rails = set(circuit.get("power_rails", []))

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

        # Power rails: standard schematic symbols only — no wires between pins
        if is_power:
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


def _pin_endpoint(comp: dict, pin: dict) -> tuple[float, float]:
    """Return (x, y) of the outer tip of a pin stub."""
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = comp["bounding_box"]["height"]
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
    h = comp["bounding_box"]["height"]
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
    h = comp["bounding_box"]["height"]
    cid = comp.get("id", "?")
    name = comp.get("name", "")
    value = comp.get("value", "")
    comp_type = comp.get("type", "").lower()

    # Component body
    group.add(dwg.rect(
        insert=(x, y),
        size=(w, h),
        fill=COMP_FILL,
        stroke=COMP_STROKE,
        stroke_width=COMP_STROKE_W,
        rx=3, ry=3,
    ))

    # Component ID (bold, top-left area)
    group.add(dwg.text(
        cid,
        insert=(x + 4, y + LABEL_FONT + 2),
        font_size=LABEL_FONT,
        font_family="monospace",
        font_weight="bold",
        fill="#222",
    ))

    # Component name (smaller, below ID)
    if name:
        group.add(dwg.text(
            name,
            insert=(x + 4, y + LABEL_FONT * 2 + 4),
            font_size=FONT_SIZE,
            font_family="monospace",
            fill="#444",
        ))

    # Value (italic, bottom of body)
    if value:
        group.add(dwg.text(
            value,
            insert=(x + 4, y + h - 4),
            font_size=FONT_SIZE,
            font_family="monospace",
            font_style="italic",
            fill="#666",
        ))

    # Pin stubs and pin labels
    for pin in comp.get("pins", []):
        _draw_pin_stub(dwg, group, comp, pin)


def _draw_pin_stub(dwg: svgwrite.Drawing, group, comp: dict, pin: dict) -> None:
    x = comp["position"]["x"]
    y = comp["position"]["y"]
    w = comp["bounding_box"]["width"]
    h = comp["bounding_box"]["height"]
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
