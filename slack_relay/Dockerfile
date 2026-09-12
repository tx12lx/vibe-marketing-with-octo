FROM google/cloud-sdk:slim

WORKDIR /app

# google/cloud-sdk:slim already ships gcloud + Python 3; this just adds the
# small Slack/relay layer on top of it.
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/*
# websocket-client is Socket Mode's actual transport -- slack_bolt does not
# always pull it in on its own, and without it SocketModeHandler.start() fails.
RUN pip3 install --no-cache-dir --break-system-packages slack_bolt websocket-client httpx

COPY main.py tunnel_manager.py ./

# This service makes no meaningful inbound connections of its own (Slack and
# the VM are both reached outbound) -- the tiny HTTP server on $PORT exists
# only to satisfy Cloud Run's own startup probe.
CMD ["python3", "main.py"]
