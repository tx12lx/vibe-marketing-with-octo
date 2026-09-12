FROM google/cloud-sdk:slim

WORKDIR /app

# google/cloud-sdk:slim already ships gcloud + Python 3; this just adds the
# small web-serving/proxying layer on top of it.
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages fastapi "uvicorn[standard]" httpx

COPY main.py tunnel_manager.py ./

# Cloud Run injects $PORT (default 8080); main.py already reads it from the
# environment rather than assuming a fixed value.
EXPOSE 8080

CMD ["python3", "main.py"]
