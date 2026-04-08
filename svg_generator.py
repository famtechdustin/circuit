"""
svg_generator.py — Generate a 2D schematic SVG from a circuit JSON dict.

Components are drawn as labeled rectangles with pin stubs on the specified side.
Nets are drawn as right-angle (Manhattan) wires between pin endpoints.
Power rails (VCC, GND, etc.) receive standard schematic symbols.
"""

import io
import svgwrite
from svgwrite import cm, mm

CANVAS_W = 1400
CANVAS_H = 1000
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

    # Build a lookup: (component_id, pin_number) → (x, y) of pin stub tip
    pin_endpoints: dict[tuple[str, int], tuple[float, float]] = {}
    for comp in components:
        for pin in comp.get("pins", []):
            ep = _pin_endpoint(comp, pin)
            pin_endpoints[(comp["id"], pin["number"])] = ep

    # Draw wires first (behind components)
    wire_group = dwg.add(dwg.g(id="wires"))
    net_label_group = dwg.add(dwg.g(id="net-labels"))
    for net in nets:
        name = net.get("name", "")
        connections = net.get("connections", [])
        is_power = name in power_rails

        endpoints = []
        for conn in connections:
            key = (conn.get("component_id", ""), conn.get("pin_number", -1))
            ep = pin_endpoints.get(key)
            if ep:
                endpoints.append(ep)

        stroke = POWER_STROKE if is_power and name.upper() not in {"GND", "AGND", "DGND"} \
            else GND_STROKE if name.upper() in {"GND", "AGND", "DGND"} \
            else WIRE_STROKE

        # Draw wires between consecutive pairs and to the first point
        if len(endpoints) >= 2:
            for i in range(len(endpoints) - 1):
                waypoints = _route_wire(endpoints[i], endpoints[i + 1])
                polyline_pts = waypoints
                wire_group.add(dwg.polyline(
                    points=polyline_pts,
                    stroke=stroke,
                    stroke_width=WIRE_W,
                    fill="none",
                    stroke_linejoin="round",
                    stroke_linecap="round",
                ))
            # Junction dots at branching nodes
            if len(endpoints) > 2:
                for ep in endpoints[1:-1]:
                    wire_group.add(dwg.circle(
                        center=ep, r=3,
                        fill=stroke,
                    ))

        # Net label near midpoint of first wire segment
        if len(endpoints) >= 2 and name not in power_rails:
            mid = _midpoint(endpoints[0], endpoints[1])
            _draw_net_label(dwg, net_label_group, name, mid[0], mid[1])

        # Power symbols at each endpoint for power rails
        if name in power_rails:
            for ep in endpoints:
                _draw_power_symbol(dwg, wire_group, name, ep[0], ep[1])

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


def _route_wire(
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> list[tuple[float, float]]:
    """
    Manhattan (right-angle) routing: go horizontal from p1 to mid-x, then vertical.
    """
    mid_x = (p1[0] + p2[0]) / 2
    return [p1, (mid_x, p1[1]), (mid_x, p2[1]), p2]


def _midpoint(
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> tuple[float, float]:
    return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)


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
