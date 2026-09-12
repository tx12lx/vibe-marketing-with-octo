FROM python:3.11-slim

WORKDIR /app

# Copy dependency manifest and source together -- this project's own package
# (core/, agents/, api/, knowledge/) must already be present for
# `pip install .` to resolve via [tool.setuptools.packages.find] in
# pyproject.toml, so a dependencies-only layer isn't possible here without
# restructuring the package layout.
COPY pyproject.toml .
COPY core/ core/
COPY agents/ agents/
COPY api/ api/
COPY knowledge/ knowledge/
COPY pydantic_schemas.py vibe_orchestrator.py web_cloud_run.py ./

RUN pip install --no-cache-dir .

# Cloud Run injects $PORT (default 8080); web_cloud_run.py already reads it
# from the environment rather than assuming a fixed value.
EXPOSE 8080

CMD ["python", "web_cloud_run.py"]
