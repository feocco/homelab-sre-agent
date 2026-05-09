FROM python:3.13-alpine

LABEL org.opencontainers.image.source="https://github.com/feocco/homelab-sre-agent"
LABEL org.opencontainers.image.description="Homelab SRE incident coordinator"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY homelab_sre_agent ./homelab_sre_agent

ENV SERVICE_HOST=0.0.0.0
ENV SERVICE_PORT=8094
ENV SRE_STATE_PATH=/app/state/sre-agent.sqlite3
ENV SRE_SERVICE_METADATA_PATH=/app/config/services.yaml
ENV SRE_DRY_RUN=true

CMD ["python", "-m", "homelab_sre_agent"]
