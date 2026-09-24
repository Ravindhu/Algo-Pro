# pov_engine/Dockerfile - own image, independent of the OpenAlgo image.
# Rebuild after upgrading pov_engine:  docker compose build pov-dash
FROM python:3.12-slim

# Layout mirrors the host: project root mounted/copied AS pov_engine package dir.
WORKDIR /app/pov_engine
COPY pyproject.toml README.md ./
COPY core ./core
COPY signals ./signals
COPY slicers ./slicers
COPY dashboard ./dashboard
COPY input ./input
RUN pip install --no-cache-dir ".[dashboard]"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata

# Default: run the Dash terminal. Override with `command:` in compose for the runner.
CMD ["python", "-m", "pov_engine.dashboard.dash_app"]