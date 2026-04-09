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
import re
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
HISTORY_WINDOW = 10  # keep last N turns to stay within context budget

# In-memory conversation histories keyed by project name.
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
      "bounding_box": {"width": 60, "height": 30},
      "pins": [
        {"number": 1, "name": "A", "direction": "passive", "side": "left", "offset": 0.5},
        {"number": 2, "name": "B", "direction": "passive", "side": "right", "offset": 0.5}
      ]
    }
  ],
  "nets": [
    {
      "name": "VCC",
      "connections": [{"component_id": "R1", "pin_number": 1}]
    }
  ],
  "power_rails": ["VCC", "GND"],
  "metadata": {"title": "My Circuit", "revision": 1, "notes": ""},
  "missing_datasheets": []
}

## Design rules

- Assign component IDs sequentially per type: R1/R2, C1/C2, U1/U2, D1/D2, etc.
- Use standardised net names: VCC, GND, 3V3, 5V, VBAT, VOUT, etc.
- Always add decoupling capacitors (100nF) on every IC VCC pin.
- pin.side must be one of: "left", "right", "top", "bottom"
- pin.offset is 0.0–1.0 (fractional position along that edge); distribute pins evenly.
- pin.direction must be one of: "input", "output", "bidirectional", "passive",
  "power_in", "power_out", "open_collector", "no_connect"
- Default bounding_box sizes:
    resistor/capacitor/diode: {"width": 60, "height": 30}
    DIP IC: {"width": 100, "height": <pin_count * 10>}
    QFP/QFN IC: {"width": 120, "height": 120}
    connector: {"width": 50, "height": <pin_count * 15>}
    transistor: {"width": 60, "height": 60}
- Keep positions non-overlapping; space components 80–120 px apart.
- ALWAYS return the COMPLETE circuit JSON on every response — never a partial diff.
- When updating in response to a follow-up, increment metadata.revision by 1.

## Datasheet context

Relevant datasheet text is injected in the user message like this:
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

## Missing datasheets

If your design requires a part whose datasheet was NOT provided in this message,
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
) -> tuple[dict, str, str]:
    """
    Run one design turn for the given project.

    Returns (circuit_dict, circuit_context_md, recreate_prompt_md).
    Raises ValueError on parse failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = _build_user_message(
        user_prompt, datasheet_texts, kicad_symbols, existing_circuit
    )

    history = get_history(project_name)
    history.append({"role": "user", "content": user_message})

    # Sliding window: trim to last HISTORY_WINDOW turns (pairs)
    if len(history) > HISTORY_WINDOW * 2:
        history = history[-(HISTORY_WINDOW * 2):]
        _histories[project_name] = history

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    raw_text = response.content[0].text
    history.append({"role": "assistant", "content": raw_text})

    circuit_dict, context_md, recreate_md = _parse_response(raw_text)
    return circuit_dict, context_md, recreate_md


def _build_user_message(
    user_prompt: str,
    datasheet_texts: dict[str, str],
    kicad_symbols: dict[str, list],
    existing_circuit: dict | None,
) -> str:
    parts = [user_prompt.strip()]

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

    if existing_circuit is not None:
        parts.append(
            "\n## Current circuit state (update this)\n"
            + json.dumps(existing_circuit, indent=2)
        )

    return "\n\n".join(parts)


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
    json_part = re.sub(r"^```[a-zA-Z]*\n?", "", json_part).rstrip("`").strip()

    # Extract outermost {...} using a bracket counter
    try:
        circuit_dict = _extract_json(json_part)
    except ValueError:
        # If delimiter was missing, try extracting JSON from the full raw response
        try:
            circuit_dict = _extract_json(raw_text)
            markdown_part = raw_text[raw_text.rfind("}") + 1:].strip()
        except ValueError:
            preview = raw_text[:400].replace("\n", " ")
            raise ValueError(
                f"Could not parse JSON from Claude's response. "
                f"First 400 chars: {preview}"
            )

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
                    return json.loads(json_str)
                except json.JSONDecodeError as e:
                    raise ValueError(f"JSON parse error: {e}\n\nRaw JSON:\n{json_str[:500]}")

    raise ValueError("Unmatched braces — could not extract JSON from response.")
