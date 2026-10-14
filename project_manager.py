"""
project_manager.py — filesystem I/O for circuit design projects.

Each project lives at projects/<name>/ and contains:
  datasheets/       — PDF datasheets (user-placed)
  kicad_symbols/    — KiCad 6+ symbol files (.kicad_sym, user-placed)
  circuit.json      — latest structured circuit state
  circuit_context.md — running circuit description
  recreate_prompt.md — single prompt to reproduce circuit
  schematic.svg     — latest generated SVG schematic
  bom_report.json   — results of the last BOM import (per-part fetch status)
  part_descriptions.json — user's description of the board and what each main part does
  token_usage.json  — cumulative Claude API token usage for the project
"""

import json
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path("projects")


@dataclass
class Project:
    name: str
    root: Path
    datasheets_dir: Path
    symbols_dir: Path
    circuit_json_path: Path
    context_md_path: Path
    recreate_md_path: Path
    svg_path: Path
    bom_report_path: Path
    part_descriptions_path: Path
    token_usage_path: Path


def _make_project(name: str) -> Project:
    root = BASE_DIR / name
    return Project(
        name=name,
        root=root,
        datasheets_dir=root / "datasheets",
        symbols_dir=root / "kicad_symbols",
        circuit_json_path=root / "circuit.json",
        context_md_path=root / "circuit_context.md",
        recreate_md_path=root / "recreate_prompt.md",
        svg_path=root / "schematic.svg",
        bom_report_path=root / "bom_report.json",
        part_descriptions_path=root / "part_descriptions.json",
        token_usage_path=root / "token_usage.json",
    )


def create_project(name: str) -> Project:
    """Create folder tree for a new project. Raises ValueError if it already exists."""
    name = name.strip()
    if not name:
        raise ValueError("Project name cannot be empty.")
    project = _make_project(name)
    if project.root.exists():
        raise ValueError(f"Project '{name}' already exists.")
    project.datasheets_dir.mkdir(parents=True, exist_ok=False)
    project.symbols_dir.mkdir(parents=True, exist_ok=False)
    return project


def open_project(name: str) -> Project:
    """Open an existing project. Raises FileNotFoundError if missing."""
    project = _make_project(name)
    if not project.root.exists():
        raise FileNotFoundError(f"Project '{name}' not found.")
    return project


def list_projects() -> list[str]:
    """Return names of all existing project folders, sorted."""
    if not BASE_DIR.exists():
        return []
    return sorted(
        p.name for p in BASE_DIR.iterdir() if p.is_dir()
    )


def list_datasheets(project: Project) -> list[str]:
    """Return PDF basenames (without .pdf) present in datasheets/."""
    if not project.datasheets_dir.exists():
        return []
    return sorted(
        p.stem for p in project.datasheets_dir.iterdir()
        if p.suffix.lower() == ".pdf"
    )


def list_kicad_symbols(project: Project) -> list[str]:
    """Return .kicad_sym basenames (without extension) present in kicad_symbols/."""
    if not project.symbols_dir.exists():
        return []
    return sorted(
        p.stem for p in project.symbols_dir.iterdir()
        if p.suffix.lower() == ".kicad_sym"
    )


def read_circuit_json(project: Project) -> dict | None:
    """Return parsed circuit JSON or None if not yet created."""
    if not project.circuit_json_path.exists():
        return None
    try:
        return json.loads(project.circuit_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_circuit_json(project: Project, data: dict) -> None:
    project.circuit_json_path.write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def read_context_md(project: Project) -> str:
    """Return circuit_context.md content, or empty string if not present."""
    if not project.context_md_path.exists():
        return ""
    return project.context_md_path.read_text(encoding="utf-8")


def write_context_md(project: Project, content: str) -> None:
    project.context_md_path.write_text(content, encoding="utf-8")


def write_recreate_prompt(project: Project, content: str) -> None:
    project.recreate_md_path.write_text(content, encoding="utf-8")


def read_svg(project: Project) -> str | None:
    """Return raw SVG string or None if not yet generated."""
    if not project.svg_path.exists():
        return None
    return project.svg_path.read_text(encoding="utf-8")


def write_svg(project: Project, svg_content: str) -> None:
    project.svg_path.write_text(svg_content, encoding="utf-8")


def read_bom_report(project: Project) -> dict | None:
    """Return the saved BOM import report or None if no BOM was imported."""
    if not project.bom_report_path.exists():
        return None
    try:
        return json.loads(project.bom_report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_bom_report(project: Project, report: dict) -> None:
    project.bom_report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def read_part_descriptions(project: Project) -> dict | None:
    """Return {"overview": str, "parts": {mpn: description}} or None if never saved."""
    if not project.part_descriptions_path.exists():
        return None
    try:
        return json.loads(project.part_descriptions_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_part_descriptions(project: Project, data: dict) -> None:
    project.part_descriptions_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_token_usage(project: Project) -> dict:
    """Return cumulative token usage, zeroed if none.

    input_tokens excludes cached tokens; cache_read_tokens and cache_write_tokens
    are counted separately (billed at ~0.1x and ~1.25x the input rate).
    """
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }
    if project.token_usage_path.exists():
        try:
            usage.update(json.loads(project.token_usage_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return usage


def add_token_usage(
    project: Project,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict:
    """Add one request's usage to the project's running total and return the new total."""
    usage = read_token_usage(project)
    usage["input_tokens"] += input_tokens
    usage["output_tokens"] += output_tokens
    usage["cache_read_tokens"] += cache_read_tokens
    usage["cache_write_tokens"] += cache_write_tokens
    usage["total_tokens"] = (
        usage["input_tokens"] + usage["output_tokens"]
        + usage["cache_read_tokens"] + usage["cache_write_tokens"]
    )
    usage["requests"] += 1
    project.token_usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    return usage
