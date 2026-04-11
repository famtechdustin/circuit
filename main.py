"""
main.py — FastAPI web application for the Circuit Design Generator.

Start with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Or via Docker:
    docker compose up
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import circuit_designer
import datasheet_reader
import kicad_parser
import part_finder
import project_manager
import svg_generator

load_dotenv()

app = FastAPI(title="Circuit Design Generator")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class NewProjectRequest(BaseModel):
    name: str


class PromptRequest(BaseModel):
    project_name: str
    prompt: str


class RegenSvgRequest(BaseModel):
    canvas_w: int | None = None
    canvas_h: int | None = None


class FetchPartsRequest(BaseModel):
    parts: list[str]


class PromptResponse(BaseModel):
    status: str                 # "ok" | "missing_datasheets" | "error"
    missing_parts: list[str]    # populated when status == "missing_datasheets"
    circuit_context: str        # updated circuit_context.md content
    message: str                # human-readable summary
    revision: int = 0


class StatusResponse(BaseModel):
    project_name: str
    datasheets: list[str]
    kicad_symbols: list[str]
    has_circuit: bool
    revision: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the single-page UI."""
    return templates.TemplateResponse("index.html", {"request": request})


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

    existing_circuit = project_manager.read_circuit_json(project)

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
            user_prompt=req.prompt,
            datasheet_texts=datasheet_texts,
            kicad_symbols=kicad_data,
            existing_circuit=existing_circuit,
        )
    except EnvironmentError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        return PromptResponse(
            status="error",
            missing_parts=[],
            circuit_context=project_manager.read_context_md(project),
            message=f"Failed to parse Claude's response: {e}",
        )
    except Exception as e:
        return PromptResponse(
            status="error",
            missing_parts=[],
            circuit_context=project_manager.read_context_md(project),
            message=f"Claude API error: {e}",
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


@app.post("/fetch-parts/{project_name}")
async def fetch_parts(project_name: str, req: FetchPartsRequest):
    """
    For each part in the list, search public sources and save:
      - PDF datasheet  → projects/<name>/datasheets/<part>.pdf
      - KiCad symbol   → projects/<name>/kicad_symbols/<part>.kicad_sym
    Returns per-part results without calling Claude.
    """
    try:
        project = project_manager.open_project(project_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    project.symbols_dir.mkdir(parents=True, exist_ok=True)

    async def _fetch_one(p: str) -> dict:
        ds_bytes, ds_status = await part_finder.find_datasheet(p)
        sym_text, sym_status = await part_finder.find_kicad_symbol(p)
        if ds_bytes:
            (project.datasheets_dir / f"{p}.pdf").write_bytes(ds_bytes)
        if sym_text:
            (project.symbols_dir / f"{p}.kicad_sym").write_text(sym_text, encoding="utf-8")
        return {"part": p, "datasheet": ds_status, "symbol": sym_status}

    import asyncio
    results = await asyncio.gather(*[_fetch_one(p) for p in req.parts])
    return {"results": list(results)}


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
