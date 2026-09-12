FROM python:3.11-slim

WORKDIR /app

COPY main.py .

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx

# Cloud Run injects $PORT (default 8080); main.py already reads it from the
# environment rather than assuming a fixed value.
EXPOSE 8080

CMD ["python", "main.py"]
