# Blue_engine/Dockerfile - own image, independent of the OpenAlgo image.
# Rebuild after upgrading Blue_engine:  docker compose build pov-dash
FROM python:3.12-slim

# Layout mirrors the host: project root mounted/copied AS blue_engine package dir.
WORKDIR /app/blue_engine
COPY pyproject.toml README.md ./
COPY core ./core
# signals/ now lives under alpha/, slicers/ under pov/ (co-located with their engine).
# Engine sub-packages live FLAT under Blue_engine/ (no intermediate engines/ dir).
COPY alpha ./alpha
COPY economics ./economics
COPY market ./market
COPY portfolio ./portfolio
COPY pov ./pov
COPY infrastructure ./infrastructure
COPY x ./x
COPY input ./input
RUN pip install --no-cache-dir ".[dashboard]"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata

# Default: run the FastAPI dashboard. Override with `command:` in compose for the runner.
CMD ["python", "-m", "blue_engine.dashboard.web.run"]