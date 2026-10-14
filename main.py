"""
main.py — FastAPI web application for the Circuit Design Generator.

Start with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Or via Docker:
    docker compose up
"""

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import bom_importer
import circuit_designer
import datasheet_reader
import eagle_exporter
import kicad_parser
import part_finder
import project_manager
import svg_generator

load_dotenv()

app = FastAPI(title="Circuit Design Generator")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Concurrent part lookups per request (Mouser calls are throttled separately)
_FETCH_CONCURRENCY = 4


@app.on_event("startup")
async def _warm_kicad_index():
    """Start downloading/indexing the KiCad libraries so the first BOM import is fast."""
    app.state.kicad_index_task = asyncio.create_task(part_finder.ensure_kicad_index())


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class NewProjectRequest(BaseModel):
    name: str


class PromptRequest(BaseModel):
    project_name: str
    prompt: str
    model: str | None = None


class RegenSvgRequest(BaseModel):
    canvas_w: int | None = None
    canvas_h: int | None = None


class FetchPartsRequest(BaseModel):
    parts: list[str]


class PartDescriptionsRequest(BaseModel):
    overview: str = ""
    parts: dict[str, str] = {}   # MPN -> what the part does in this design


class PromptResponse(BaseModel):
    status: str                 # "ok" | "missing_datasheets" | "error"
    missing_parts: list[str]    # populated when status == "missing_datasheets"
    circuit_context: str        # updated circuit_context.md content
    message: str                # human-readable summary
    revision: int = 0
    replacements: list[dict] = []   # parts swapped out by a "replace X with Y" prompt


class StatusResponse(BaseModel):
    project_name: str
    datasheets: list[str]
    kicad_symbols: list[str]
    has_circuit: bool
    revision: int
    token_usage: dict


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the single-page UI."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/models")
async def list_models():
    """Return the Claude models the UI can choose from."""
    return {
        "default": circuit_designer.MODEL,
        "models": [{"id": k, "label": v} for k, v in circuit_designer.MODELS.items()],
    }


@app.get("/projects", response_model=list[str])
async def list_projects():
    """Return names of all existing projects."""
    return project_manager.list_projects()


@app.post("/projects")
async def create_project(req: NewProjectRequest):
    """Create a new project folder."""
    try:
        project = project_manager.create_project(req.name)
        return {"name": project.name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/status/{project_name}", response_model=StatusResponse)
async def get_status(project_name: str):
    """Return file inventory and circuit state for a project."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    circuit = project_manager.read_circuit_json(project)
    revision = circuit.get("metadata", {}).get("revision", 0) if circuit else 0

    return StatusResponse(
        project_name=project.name,
        datasheets=project_manager.list_datasheets(project),
        kicad_symbols=project_manager.list_kicad_symbols(project),
        has_circuit=circuit is not None,
        revision=revision,
        token_usage=project_manager.read_token_usage(project),
    )


@app.post("/prompt", response_model=PromptResponse)
async def process_prompt(req: PromptRequest):
    """
    Core workflow: receive a prompt, run one Claude design turn,
    update project files, return the result.
    """
    try:
        project = project_manager.open_project(req.project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if req.model and req.model not in circuit_designer.MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model '{req.model}'.")

    existing_circuit = project_manager.read_circuit_json(project)

    # Swap parts before loading files so the new part's datasheet/symbol are sent
    replacements = await _apply_part_replacements(project, req.prompt)
    prompt = req.prompt
    if replacements:
        prompt += "\n\n" + "\n".join(
            f"[PART_REPLACEMENT] {r['old']} has been replaced by {r['new']}"
            + (f" ({', '.join(r['refdes'])})" if r["refdes"] else "")
            + f". Update every component that used {r['old']} to {r['new']}, using the "
            f"{r['new']} datasheet/KiCad pins. Its design-intent notes carry over."
            for r in replacements
        )

    # Load KiCad symbol data for all uploaded .kicad_sym files
    kicad_data: dict[str, list] = {}
    for stem in project_manager.list_kicad_symbols(project):
        symbols = kicad_parser.get_kicad_symbols_for_part(project, stem)
        if symbols:
            kicad_data[stem] = symbols

    # Gather available datasheet texts for all known parts
    # We send all available datasheets; Claude will use what it needs.
    known_parts = project_manager.list_datasheets(project)
    datasheet_texts = datasheet_reader.extract_text_for_parts(project, known_parts)

    # Call Claude
    try:
        circuit_dict, context_md, recreate_md = circuit_designer.design_step(
            project_name=req.project_name,
            user_prompt=prompt,
            datasheet_texts=datasheet_texts,
            kicad_symbols=kicad_data,
            existing_circuit=existing_circuit,
            design_intent=_design_intent(project),
            on_usage=lambda i, o, cr, cw: project_manager.add_token_usage(project, i, o, cr, cw),
            model=req.model,
        )
    except EnvironmentError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        return PromptResponse(
            status="error",
            missing_parts=[],
            circuit_context=project_manager.read_context_md(project),
            message=f"Failed to parse Claude's response: {e}",
            replacements=replacements,
        )
    except Exception as e:
        return PromptResponse(
            status="error",
            missing_parts=[],
            circuit_context=project_manager.read_context_md(project),
            message=f"Claude API error: {e}",
            replacements=replacements,
        )

    # Check if Claude flagged missing datasheets
    missing = circuit_dict.get("missing_datasheets", [])
    if missing:
        return PromptResponse(
            status="missing_datasheets",
            missing_parts=missing,
            circuit_context=project_manager.read_context_md(project),
            message=(
                f"Claude needs datasheets for: {', '.join(missing)}. "
                f"Add the PDF(s) to projects/{req.project_name}/datasheets/ and retry."
            ),
            replacements=replacements,
        )

    # Commit the design to disk
    project_manager.write_circuit_json(project, circuit_dict)
    if context_md:
        project_manager.write_context_md(project, context_md)
    if recreate_md:
        project_manager.write_recreate_prompt(project, recreate_md)

    # Regenerate SVG
    try:
        svg_content = svg_generator.generate_svg(circuit_dict)
        project_manager.write_svg(project, svg_content)
    except Exception as e:
        # SVG failure is non-fatal; circuit JSON is already saved
        pass

    revision = circuit_dict.get("metadata", {}).get("revision", 1)
    return PromptResponse(
        status="ok",
        missing_parts=[],
        circuit_context=context_md or project_manager.read_context_md(project),
        message=f"Circuit updated (revision {revision}).",
        revision=revision,
        replacements=replacements,
    )


@app.get("/svg/{project_name}")
async def get_svg(project_name: str):
    """Return the current schematic SVG."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    svg = project_manager.read_svg(project)
    if svg is None:
        raise HTTPException(status_code=404, detail="No schematic generated yet.")

    return Response(content=svg, media_type="image/svg+xml")


@app.get("/circuit/{project_name}")
async def get_circuit(project_name: str):
    """Return the current circuit JSON."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    circuit = project_manager.read_circuit_json(project)
    if circuit is None:
        raise HTTPException(status_code=404, detail="No circuit designed yet.")

    return JSONResponse(content=circuit)


@app.get("/context/{project_name}")
async def get_context(project_name: str):
    """Return the circuit_context.md and recreate_prompt.md content."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    context = project_manager.read_context_md(project)
    recreate = (
        project.recreate_md_path.read_text(encoding="utf-8")
        if project.recreate_md_path.exists()
        else ""
    )
    return {"circuit_context": context, "recreate_prompt": recreate}


@app.delete("/projects/{project_name}/history")
async def clear_history(project_name: str):
    """Clear the in-memory conversation history for a project."""
    circuit_designer.clear_history(project_name)
    return {"cleared": True, "project": project_name}


@app.post("/regenerate-svg/{project_name}")
async def regenerate_svg(project_name: str, req: RegenSvgRequest = RegenSvgRequest()):
    """Re-render the SVG from the current circuit.json without calling Claude."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    circuit = project_manager.read_circuit_json(project)
    if circuit is None:
        raise HTTPException(status_code=404, detail="No circuit.json found for this project.")

    try:
        svg_content = svg_generator.generate_svg(circuit, min_w=req.canvas_w, min_h=req.canvas_h)
        project_manager.write_svg(project, svg_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SVG generation failed: {e}")

    return {"status": "ok"}


async def _fetch_part(project: project_manager.Project, part: str) -> dict:
    """
    Fetch the datasheet and KiCad symbol for one part and save them:
      - PDF datasheet  → projects/<name>/datasheets/<part>.pdf
      - KiCad symbol   → projects/<name>/kicad_symbols/<part>.kicad_sym
    Files already present in the project are kept and reported as found.
    """
    stem = part_finder.safe_filename(part)
    ds_path = project.datasheets_dir / f"{stem}.pdf"
    sym_path = project.symbols_dir / f"{stem}.kicad_sym"
    result = {
        "part": part,
        "datasheet": "found", "datasheet_source": "existing", "product_url": "",
        "symbol": "found", "symbol_match": "",
    }

    if not ds_path.exists():
        ds = await part_finder.find_datasheet(part)
        if ds.pdf:
            ds_path.write_bytes(ds.pdf)
        result.update(datasheet=ds.status, datasheet_source=ds.source, product_url=ds.product_url)

    if not sym_path.exists():
        sym = await part_finder.find_kicad_symbol(part)
        if sym.text:
            sym_path.write_text(sym.text, encoding="utf-8")
        result.update(symbol=sym.status, symbol_match=sym.match)

    result["flagged"] = result["datasheet"] != "found" or result["symbol"] != "found"
    return result


async def _fetch_parts(project: project_manager.Project, parts: list[str]) -> list[dict]:
    project.datasheets_dir.mkdir(parents=True, exist_ok=True)
    project.symbols_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(p: str) -> dict:
        async with sem:
            return await _fetch_part(project, p)

    return list(await asyncio.gather(*[_one(p) for p in parts]))


def _summarize(rows: list[dict]) -> dict:
    searched = [r for r in rows if not r.get("generic")]
    return {
        "total": len(rows),
        "searched": len(searched),
        "flagged": sum(1 for r in searched if r.get("flagged")),
        "generic": len(rows) - len(searched),
    }


@app.post("/fetch-parts/{project_name}")
async def fetch_parts(project_name: str, req: FetchPartsRequest):
    """
    For each part in the list, fetch its datasheet and KiCad symbol into the project.
    If a BOM report exists, matching rows are updated with the new results.
    Returns per-part results without calling Claude.
    """
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    results = await _fetch_parts(project, req.parts)

    report = project_manager.read_bom_report(project)
    if report:
        by_part = {r["part"].upper(): r for r in results}
        for row in report.get("rows", []):
            fresh = by_part.get(row["mpn"].upper())
            if fresh and not row.get("generic"):
                row.update({k: v for k, v in fresh.items() if k != "part" and v != ""})
        report["summary"] = _summarize(report.get("rows", []))
        project_manager.write_bom_report(project, report)

    return {"results": results}


@app.post("/import-bom/{project_name}")
async def import_bom(project_name: str, file: UploadFile = File(...)):
    """
    Import a DipTrace BOM CSV, fetch the datasheet and KiCad symbol for every
    non-generic part, and save the per-part results to bom_report.json.
    """
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")

    try:
        bom_rows = bom_importer.parse_diptrace_csv(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not bom_rows:
        raise HTTPException(status_code=400, detail="No parts found in the BOM.")

    to_fetch = [r.mpn for r in bom_rows if not r.generic]
    fetched = {r["part"]: r for r in await _fetch_parts(project, to_fetch)}

    rows = []
    for b in bom_rows:
        row = b.to_dict()
        if b.generic:
            row.update(datasheet="skipped", symbol="skipped", flagged=False)
        else:
            row.update({k: v for k, v in fetched[b.mpn].items() if k != "part"})
        rows.append(row)

    report = {
        "filename": Path(file.filename or "bom.csv").name,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "summary": _summarize(rows),
    }
    project_manager.write_bom_report(project, report)
    return report


@app.get("/bom/{project_name}")
async def get_bom(project_name: str):
    """Return the last BOM import report for a project."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    report = project_manager.read_bom_report(project)
    if report is None:
        raise HTTPException(status_code=404, detail="No BOM imported yet.")
    return report


def _design_intent(project: project_manager.Project) -> dict | None:
    """Combine saved part descriptions with BOM RefDes for the Claude prompt."""
    saved = project_manager.read_part_descriptions(project)
    if not saved:
        return None
    report = project_manager.read_bom_report(project) or {}
    refdes = {r["mpn"].upper(): r.get("refdes", []) for r in report.get("rows", [])}
    return {
        "overview": saved.get("overview", ""),
        "parts": [
            {"mpn": mpn, "refdes": refdes.get(mpn.upper(), []), "description": desc}
            for mpn, desc in saved.get("parts", {}).items()
        ],
    }


# "replace|swap [out] [the] <old> [up to 2 words / (note)] with|by|for [a|an|the] <new>"
_REPLACE_TMPL = (
    r"\b(?:replace|swap(?:\s+out)?)\s+(?:the\s+)?(?<![\w-]){old}(?![\w-])"
    r"(?:\s+\([^)]*\))?(?:\s+[A-Za-z]+){{0,2}}?"
    r"\s+(?:with|by|for)\s+(?:an?\s+|the\s+)?([A-Za-z0-9][\w\-./#+]*)"
)


def _find_part_replacements(prompt: str, aliases: dict[str, str]) -> list[tuple[str, str]]:
    """
    Find "replace <old> with <new>" requests in a prompt.
    aliases maps an upper-cased MPN/RefDes/datasheet name to the listed MPN it refers to.
    Returns [(old_mpn, new_mpn)]; <new> must look like a part number (letters + digits).
    """
    found: dict[str, tuple[str, str]] = {}
    for alias in sorted(aliases, key=len, reverse=True):
        old = aliases[alias]
        if old.upper() in found:
            continue
        m = re.search(_REPLACE_TMPL.format(old=re.escape(alias)), prompt, re.IGNORECASE)
        if not m:
            continue
        new = m.group(1).rstrip(".,;:!?)")
        if (len(new) >= 3 and re.search(r"[A-Za-z]", new) and re.search(r"\d", new)
                and new.upper() != old.upper()):
            found[old.upper()] = (old, new)
    return list(found.values())


def _delete_part_files(directory: Path, ext: str, part: str) -> None:
    if not directory.exists():
        return
    stems = {part.upper(), part_finder.safe_filename(part).upper()}
    for p in directory.iterdir():
        if p.suffix.lower() == ext and p.stem.upper() in stems:
            p.unlink()


async def _apply_part_replacements(project: project_manager.Project, prompt: str) -> list[dict]:
    """
    Handle "replace <old> with <new>" in a prompt: delete the old part's datasheet and
    KiCad symbol, fetch the new part's, and rename the part in the BOM report and part
    descriptions so its RefDes and notes carry over to every later prompt.
    """
    report = project_manager.read_bom_report(project)
    descs = project_manager.read_part_descriptions(project)
    rows = report.get("rows", []) if report else []

    aliases: dict[str, str] = {}
    for stem in project_manager.list_datasheets(project):
        aliases[stem.upper()] = stem
    for mpn in (descs or {}).get("parts", {}):
        aliases[mpn.upper()] = mpn
    for row in rows:
        if row.get("generic"):
            continue
        aliases[row["mpn"].upper()] = row["mpn"]
        for ref in row.get("refdes", []):
            aliases[ref.upper()] = row["mpn"]

    pairs = _find_part_replacements(prompt, aliases)
    if not pairs:
        return []

    for old, _new in pairs:
        _delete_part_files(project.datasheets_dir, ".pdf", old)
        _delete_part_files(project.symbols_dir, ".kicad_sym", old)
    fetched = {r["part"]: r for r in await _fetch_parts(project, [n for _, n in pairs])}

    results = []
    for old, new in pairs:
        fresh = {k: v for k, v in fetched[new].items() if k != "part"}
        refdes: list[str] = []

        old_row = next((r for r in rows if r["mpn"].upper() == old.upper()), None)
        if old_row:
            refdes = list(old_row.get("refdes", []))
            existing = next((r for r in rows if r["mpn"].upper() == new.upper()), None)
            if existing:
                existing["refdes"] += [r for r in refdes if r not in existing["refdes"]]
                existing["qty"] = existing.get("qty", 0) + old_row.get("qty", 0)
                existing.update(fresh)
                rows.remove(old_row)
            else:
                old_row.update(fresh, mpn=new, manufacturer="", replaces=old)

        if descs and descs.get("parts"):
            parts = descs["parts"]
            old_key = next((k for k in parts if k.upper() == old.upper()), None)
            if old_key:
                new_key = next((k for k in parts if k.upper() == new.upper()), None)
                if new_key:
                    note = parts.pop(old_key)
                    parts[new_key] = parts[new_key] or note
                else:
                    descs["parts"] = {(new if k == old_key else k): v for k, v in parts.items()}

        results.append({"old": old, "new": new, "refdes": refdes,
                        "datasheet": fresh["datasheet"], "symbol": fresh["symbol"]})

    if report:
        report["summary"] = _summarize(rows)
        project_manager.write_bom_report(project, report)
    if descs:
        project_manager.write_part_descriptions(project, descs)
    return results


@app.get("/part-descriptions/{project_name}")
async def get_part_descriptions(project_name: str):
    """Return the saved board overview and per-part descriptions."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    saved = project_manager.read_part_descriptions(project)
    if saved is None:
        return {"overview": "", "parts": {}, "saved": False}
    return {**saved, "saved": True}


@app.post("/part-descriptions/{project_name}")
async def save_part_descriptions(project_name: str, req: PartDescriptionsRequest):
    """Save the board overview and what each main part does; sent to Claude with every prompt."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    data = {
        "overview": req.overview.strip(),
        "parts": {mpn: d.strip() for mpn, d in req.parts.items() if d.strip()},
    }
    project_manager.write_part_descriptions(project, data)
    return data


# ---------------------------------------------------------------------------
# Upload routes
# ---------------------------------------------------------------------------

@app.post("/upload/datasheet/{project_name}")
async def upload_datasheet(project_name: str, files: list[UploadFile] = File(...)):
    """Accept one or more PDF uploads and save to projects/<name>/datasheets/."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    saved = []
    for upload in files:
        filename = Path(upload.filename).name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"'{filename}' is not a PDF.")
        (project.datasheets_dir / filename).write_bytes(await upload.read())
        saved.append(filename)

    return {"saved": saved}


@app.post("/upload/symbol/{project_name}")
async def upload_symbol(project_name: str, files: list[UploadFile] = File(...)):
    """Accept one or more .kicad_sym uploads and save to projects/<name>/kicad_symbols/."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Create directory in case this is a legacy project created before this feature
    project.symbols_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for upload in files:
        filename = Path(upload.filename).name
        if Path(filename).suffix.lower() != ".kicad_sym":
            raise HTTPException(status_code=400, detail=f"'{filename}' is not a .kicad_sym file.")
        dest = project.symbols_dir / filename
        dest.write_bytes(await upload.read())
        saved.append(filename)

    return {"saved": saved}


# ---------------------------------------------------------------------------
# Download routes
# ---------------------------------------------------------------------------

@app.get("/download/context/{project_name}")
async def download_context(project_name: str):
    """Serve circuit_context.md as a downloadable attachment."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not project.context_md_path.exists():
        raise HTTPException(status_code=404, detail="No circuit context generated yet.")

    return FileResponse(
        path=project.context_md_path,
        media_type="text/markdown",
        filename=f"{project_name}_circuit_context.md",
    )


@app.get("/download/recreate/{project_name}")
async def download_recreate(project_name: str):
    """Serve recreate_prompt.md as a downloadable attachment."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not project.recreate_md_path.exists():
        raise HTTPException(status_code=404, detail="No recreate prompt generated yet.")

    return FileResponse(
        path=project.recreate_md_path,
        media_type="text/markdown",
        filename=f"{project_name}_recreate_prompt.md",
    )


@app.get("/download/circuit/{project_name}")
async def download_circuit_file(project_name: str):
    """Serve circuit.json as a downloadable attachment."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not project.circuit_json_path.exists():
        raise HTTPException(status_code=404, detail="No circuit designed yet.")

    return FileResponse(
        path=project.circuit_json_path,
        media_type="application/json",
        filename=f"{project_name}_circuit.json",
    )


@app.get("/download/eagle/{project_name}")
async def download_eagle(project_name: str):
    """Serve the circuit as an EAGLE XML schematic for import into DipTrace."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    circuit = project_manager.read_circuit_json(project)
    if circuit is None:
        raise HTTPException(status_code=404, detail="No circuit designed yet.")

    report = project_manager.read_bom_report(project) or {}
    try:
        sch = eagle_exporter.generate_eagle_sch(circuit, report.get("rows"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"EAGLE export failed: {e}")

    return Response(
        content=sch,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{project_name}.sch"'},
    )


# ---------------------------------------------------------------------------
# File deletion routes
# ---------------------------------------------------------------------------

@app.delete("/files/datasheet/{project_name}/{filename}")
async def delete_datasheet(project_name: str, filename: str):
    """Delete a specific datasheet PDF."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    target = (project.datasheets_dir / filename).resolve()
    if project.datasheets_dir.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"'{filename}' not found.")

    target.unlink()
    return {"deleted": filename}


@app.delete("/files/symbol/{project_name}/{filename}")
async def delete_symbol_file(project_name: str, filename: str):
    """Delete a specific KiCad symbol file."""
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    target = (project.symbols_dir / filename).resolve()
    if project.symbols_dir.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"'{filename}' not found.")

    target.unlink()
    return {"deleted": filename}
