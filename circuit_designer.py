"""
circuit_designer.py — Claude API integration for iterative circuit design.

Manages per-project conversation history (in-memory) and sends structured
prompts to Claude, returning a (circuit_dict, context_md, recreate_md) tuple.

Output contract with Claude:
  1. Raw JSON (no markdown fences) matching the circuit schema.
  2. Literal delimiter: ---MARKDOWN---
  3. ### Circuit Context  (free-text description)
  4. ### Recreate Prompt  (single self-contained prompt)
"""

import json
import os
import time
import re
from pathlib import Path
from typing import Callable

import anthropic

MODEL = "claude-sonnet-5-5"
# Models offered in the UI dropdown: id -> display label.
# Every model listed must support at least MAX_TOKENS output tokens.
MODELS = {
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
}
MAX_TOKENS = 50000
# Max turns kept in history. When exceeded, history is cut to half this size in
# one go (rather than sliding one turn at a time) so the cached prefix stays
# identical between trims.
HISTORY_WINDOW = 10

CACHE = {"type": "ephemeral"}

# In-memory conversation histories keyed by project name. Entries are lean:
# user turns hold only the prompt text and assistant turns only the circuit
# context summary. Reference material and the full circuit JSON are sent
# fresh each turn instead.
_histories: dict[str, list[dict]] = {}

SYSTEM_PROMPT = """You are a professional electronic circuit design assistant.
You help users design circuits iteratively through natural-language conversation.

## Output format (MANDATORY)

Every response MUST follow this exact structure with absolutely no deviations.
Do NOT add any text, explanation, or code fences before or after the JSON.
Do NOT wrap the JSON in ```json or any other markdown formatting.

1. A single raw JSON object starting with { on the very first character.
2. Immediately followed by the exact delimiter on its own line: ---MARKDOWN---
3. Followed by two subsections:

### Circuit Context
A concise technical description of the complete circuit as currently designed:
purpose, topology, key component choices, power architecture, notable design decisions.

### Recreate Prompt
A single self-contained prompt a future user could paste to reproduce this exact
circuit from scratch, including all part numbers/values, net names, and topology.

## JSON schema

The JSON must match this structure exactly:
{
  "components": [
    {
      "id": "R1",
      "name": "10k Pull-up",
      "type": "resistor",
      "value": "10k",
      "footprint": "0402",
      "position": {"x": 200, "y": 150},
      "bounding_box": {"width": 60, "height": 45},
      "supports": {"component_id": "U1", "pin_number": 3},
      "pins": [
        {"number": 1, "name": "A", "direction": "passive", "side": "left", "offset": 0.5},
        {"number": 2, "name": "B", "direction": "passive", "side": "right", "offset": 0.5}
      ]
    }
  ],
  "nets": [
    {
      "name": "MCU_Vcc",
      "connections": [{"component_id": "R1", "pin_number": 1}]
    }
  ],
  "power_rails": ["MCU_Vcc", "GND"],
  "metadata": {"title": "My Circuit", "revision": 1, "notes": ""},
  "missing_datasheets": []
}

## Design rules

- Assign component IDs sequentially per type: R1/R2, C1/C2, U1/U2, D1/D2, etc.
- Net naming (required): FirstLetterCaps parts joined by "_". Prefer one of:
    1) {Source IC or rail}_{Signal or voltage} — name from the driving output
       (important IC out pin + what it drives), e.g. MCU_Tx, VBatt_3v3, Reg_Vout
    2) {Target IC}_{Signal or voltage} — when naming from the load/input side,
       e.g. MCU_Rx, Amp_InP
    3) {Signal}_{PinName} when the net is clearly that pin's net, e.g. Rx_UART0
  Examples: VBatt_3v3, VBus_VDivider, MCU_Rx, Esc_Pwm, Usb_DPlus.
  Keep GND (and AGND/DGND if split) as short rail names. Avoid spaces and
  generic names like NET1, N$1, or bare VCC when a source/target is known.
- Always add decoupling capacitors (100nF) on every IC VCC / power_in pin.
  Add a larger bulk/filter capacitor on power_out or rail inputs when the
  datasheet or design calls for it (e.g. 10µF high-cap + 100nF low-cap).
- Support parts: decoupling/bulk caps and pull-up/pull-down resistors that
  exist to serve one pin of an important part MUST include:
    "supports": {"component_id": "<host id>", "pin_number": <host pin>}
  Orient that support so the pin sharing the host's signal/power net faces
  the host pin (set pin.side accordingly); the other pin faces outward
  toward GND or the rail.
- pin.side must be one of: "left", "right", "top", "bottom"
- pin.offset is 0.0–1.0 (fractional position along that edge); distribute pins evenly.
- pin.direction must be one of: "input", "output", "bidirectional", "passive",
  "power_in", "power_out", "open_collector", "no_connect"
- Default bounding_box sizes:
    resistor/capacitor/diode/ferrite/inductor (2-pin): {"width": 70, "height": 45}
    DIP IC (≤16 pins): {"width": 120, "height": <pin_count * 12>}
    DIP IC (>16 pins): {"width": 140, "height": <pin_count * 12>}
    QFP/QFN IC: {"width": 160, "height": 160}
    connector: {"width": 70, "height": <pin_count * 18>}
    transistor: {"width": 70, "height": 70}
- Canvas is 2000 × 1400 px. Spread primary components across it generously.
- Keep positions non-overlapping; space primary (non-support) components
  220–320 px apart. Support parts are exempt — place them adjacent to the
  host pin they support (roughly 40–80 px outside that pin stub).
- Start first component no closer than (120, 120) from the top-left.
- ALWAYS return the COMPLETE circuit JSON on every response — never a partial diff.
- When updating in response to a follow-up, increment metadata.revision by 1.

## Datasheet context

Relevant datasheet text is provided in the project reference material like this:
  [DATASHEET: PartName]
  <extracted text>
  [/DATASHEET]

Use pin tables, recommended application circuits, and absolute maximum ratings
from the datasheet when assigning pin numbers, names, and connections.

## KiCad symbol data

When a KiCad symbol file has been uploaded for a component, its pin data appears as:
  [KICAD_SYMBOL: ComponentName]
  Pins (N total):
    Pin 1 (VCC) — power_in
    Pin 2 (GND) — power_in
    Pin 3 (IN+) — input
    Pin 4 (OUT) — output
    ...
  Suggested bounding_box: width=Wpx height=Hpx
  [/KICAD_SYMBOL]

Use this data to:
- Assign the EXACT pin numbers and names from the KiCad symbol.
- Set pin.direction from the KiCad pin type (map directly: power_in→power_in,
  power_out→power_out, input→input, output→output, bidirectional→bidirectional,
  passive→passive, no_connect→no_connect, open_collector→open_collector).
- Use the suggested bounding_box dimensions for that component.
KiCad pin data takes precedence over defaults for pin assignments.
Datasheets (if also provided) supply electrical specs, application circuits,
and support component selection — but do NOT override KiCad pin numbers/names.

## Design intent

When the user has described the board and the role of its main parts, it appears as:
  [DESIGN_INTENT]
  Overview: <what the board does>
  - <PartNumber> (<RefDes>): <what the part does in this design>
  [/DESIGN_INTENT]

Treat this as the authoritative statement of what each part is for. Use it to decide
how parts connect to each other and which datasheet application circuits apply.

## Missing datasheets

If your design requires a part whose datasheet was NOT provided in the reference material,
set "missing_datasheets": ["PartName", ...] in the JSON.
The server will reject the design and ask the user to provide the datasheet.
"""


def get_history(project_name: str) -> list[dict]:
    return _histories.setdefault(project_name, [])


def clear_history(project_name: str) -> None:
    _histories[project_name] = []


def design_step(
    project_name: str,
    user_prompt: str,
    datasheet_texts: dict[str, str],
    kicad_symbols: dict[str, list],
    existing_circuit: dict | None,
    design_intent: dict | None = None,
    on_usage: Callable[[int, int, int, int], None] | None = None,
    model: str | None = None,
) -> tuple[dict, str, str]:
    """
    Run one design turn for the given project.

    design_intent: {"overview": str, "parts": [{"mpn", "refdes", "description"}]}
    model: one of MODELS; defaults to MODEL.
    on_usage: called with (input_tokens, output_tokens, cache_read_tokens,
              cache_write_tokens) once Claude responds, before parsing, so usage
              is recorded even if parsing fails. input_tokens excludes cached tokens.

    Returns (circuit_dict, circuit_context_md, recreate_prompt_md).
    Raises ValueError on parse failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)

    # Prompt caching: everything up to each cache_control marker is cached, so
    # stable content goes first — system prompt, then project reference
    # material, then prior turns — and the per-turn message last.
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE}]
    reference = _build_reference_block(datasheet_texts, kicad_symbols, design_intent)
    if reference:
        system.append({"type": "text", "text": reference, "cache_control": CACHE})

    history = get_history(project_name)
    if len(history) > HISTORY_WINDOW * 2:
        del history[:-HISTORY_WINDOW]

    messages = [dict(m) for m in history]
    if messages:
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"], "cache_control": CACHE}
        ]
    messages.append(
        {"role": "user", "content": _build_turn_message(user_prompt, existing_circuit)}
    )

    def _stream_once(msgs: list[dict]):
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with client.messages.stream(
                    model=model or MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=msgs,
                ) as stream:
                    return stream.get_final_message()
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))  # 2 s, 4 s
        raise last_exc

    def _record_usage(final) -> None:
        if not on_usage:
            return
        u = final.usage
        on_usage(
            u.input_tokens,
            u.output_tokens,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0,
        )

    final = _stream_once(messages)
    _record_usage(final)
    raw_text = "".join(b.text for b in final.content if b.type == "text")
    stop_reason = getattr(final, "stop_reason", None)

    try:
        circuit_dict, context_md, recreate_md = _parse_response(raw_text)
    except ValueError as first_err:
        # If output hit the token cap mid-JSON, ask Claude to continue once.
        if stop_reason == "max_tokens":
            cont_messages = [dict(m) for m in messages]
            cont_messages.append({"role": "assistant", "content": raw_text})
            cont_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response was truncated mid-output. Continue "
                        "EXACTLY from where you left off — emit only the remaining "
                        "characters, with no preamble and no repetition of prior text."
                    ),
                }
            )
            cont = _stream_once(cont_messages)
            _record_usage(cont)
            raw_text += "".join(b.text for b in cont.content if b.type == "text")
            stop_reason = getattr(cont, "stop_reason", None)
            try:
                circuit_dict, context_md, recreate_md = _parse_response(raw_text)
            except ValueError as e:
                _save_failed_response(project_name, raw_text, stop_reason)
                raise ValueError(
                    f"Claude's response was truncated (hit max_tokens={MAX_TOKENS}) "
                    f"and could not be parsed even after one continuation. "
                    f"Try a simpler prompt or a model with a larger output limit. "
                    f"Detail: {e}"
                ) from e
        else:
            _save_failed_response(project_name, raw_text, stop_reason)
            raise ValueError(
                f"{first_err} (stop_reason={stop_reason!r}). "
                f"Raw response saved to projects/{project_name}/last_claude_response.txt"
            ) from first_err

    history.append({"role": "user", "content": user_prompt.strip()})
    history.append({"role": "assistant", "content": context_md or "(circuit updated)"})

    return circuit_dict, context_md, recreate_md


def _build_turn_message(user_prompt: str, existing_circuit: dict | None) -> str:
    parts = [user_prompt.strip()]
    if existing_circuit is not None:
        parts.append(
            "\n## Current circuit state (update this)\n"
            + json.dumps(existing_circuit, indent=2)
        )
    return "\n\n".join(parts)


def _build_reference_block(
    datasheet_texts: dict[str, str],
    kicad_symbols: dict[str, list],
    design_intent: dict | None = None,
) -> str:
    """Project reference material. Must be deterministic so it stays cacheable."""
    parts = []

    if design_intent and (design_intent.get("overview") or design_intent.get("parts")):
        lines = ["[DESIGN_INTENT]"]
        if design_intent.get("overview"):
            lines.append(f"Overview: {design_intent['overview']}")
        for p in design_intent.get("parts", []):
            refs = f" ({', '.join(p['refdes'])})" if p.get("refdes") else ""
            lines.append(f"- {p['mpn']}{refs}: {p['description']}")
        lines.append("[/DESIGN_INTENT]")
        parts.append("\n## Design intent (from user)\n")
        parts.append("\n".join(lines))

    if datasheet_texts:
        parts.append("\n## Provided datasheets\n")
        for name, text in datasheet_texts.items():
            parts.append(f"[DATASHEET: {name}]\n{text}\n[/DATASHEET]")

    if kicad_symbols:
        parts.append("\n## KiCad symbol data\n")
        for _file_stem, symbols in kicad_symbols.items():
            for sym in symbols:
                lines = [f"[KICAD_SYMBOL: {sym.name}]"]
                lines.append(f"Pins ({len(sym.pins)} total):")
                for pin in sym.pins:
                    lines.append(f"  Pin {pin.number} ({pin.name}) — {pin.pin_type}")
                lines.append(
                    f"Suggested bounding_box: "
                    f"width={sym.bounding_box['width']}px "
                    f"height={sym.bounding_box['height']}px"
                )
                lines.append("[/KICAD_SYMBOL]")
                parts.append("\n".join(lines))

    if not parts:
        return ""
    return "# Project reference material\n\n" + "\n\n".join(parts)


def _save_failed_response(
    project_name: str, raw_text: str, stop_reason: str | None
) -> None:
    """Best-effort dump of an unparseable Claude reply for debugging."""
    try:
        path = Path("projects") / project_name / "last_claude_response.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"stop_reason={stop_reason!r} length={len(raw_text)}\n\n"
        path.write_text(header + raw_text, encoding="utf-8")
    except OSError:
        pass


def _parse_response(raw_text: str) -> tuple[dict, str, str]:
    """
    Split on ---MARKDOWN--- delimiter.
    Part 0: extract outermost JSON object.
    Part 1: split on '### Recreate Prompt' to get context_md and recreate_md.
    """
    parts = raw_text.split("---MARKDOWN---", 1)
    json_part = parts[0].strip()
    markdown_part = parts[1].strip() if len(parts) > 1 else ""

    # Strip markdown code fences if Claude wrapped the JSON (e.g. ```json ... ```)
    json_part = re.sub(r"^```[a-zA-Z]*\n?", "", json_part)
    json_part = re.sub(r"\n?```\s*$", "", json_part).strip()

    errors: list[str] = []
    try:
        circuit_dict = _extract_json(json_part)
    except ValueError as e1:
        errors.append(str(e1))
        # If delimiter was missing, try extracting JSON from the full raw response
        try:
            circuit_dict = _extract_json(raw_text)
            markdown_part = raw_text[raw_text.rfind("}") + 1:].strip()
        except ValueError as e2:
            errors.append(str(e2))
            preview = raw_text[:400].replace("\n", " ")
            detail = " | ".join(errors)
            raise ValueError(
                f"Could not parse JSON from Claude's response. {detail}. "
                f"First 400 chars: {preview}"
            ) from e2

    # Split markdown into context and recreate sections
    context_md = markdown_part
    recreate_md = ""
    recreate_marker = "### Recreate Prompt"
    if recreate_marker in markdown_part:
        idx = markdown_part.index(recreate_marker)
        context_md = markdown_part[:idx].strip()
        recreate_md = markdown_part[idx + len(recreate_marker):].strip()

    # Clean up '### Circuit Context' header if present
    context_marker = "### Circuit Context"
    if context_md.startswith(context_marker):
        context_md = context_md[len(context_marker):].strip()

    return circuit_dict, context_md, recreate_md


def _loads_json(json_str: str) -> dict:
    """Parse JSON, tolerating common LLM trailing-comma mistakes."""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        repaired = re.sub(r",(\s*[}\]])", r"\1", json_str)
        return json.loads(repaired)


def _extract_json(text: str) -> dict:
    """
    Find the outermost {...} block in text using a bracket counter and parse it.
    Raises ValueError if no valid JSON object is found.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in Claude's response.")

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_str = text[start:i + 1]
                try:
                    return _loads_json(json_str)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"JSON parse error: {e}. "
                        f"Around: {json_str[max(0, e.pos - 40): e.pos + 40]!r}"
                    ) from e

    raise ValueError(
        f"Unmatched braces - response likely truncated "
        f"(opened at index {start}, depth still {depth}, length {len(text)})."
    )
