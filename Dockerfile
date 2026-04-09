FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py circuit_designer.py project_manager.py \
     datasheet_reader.py kicad_parser.py svg_generator.py ./

COPY templates/ templates/
COPY static/ static/

# projects/ is mounted as a volume — do NOT copy it
RUN mkdir -p projects

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
