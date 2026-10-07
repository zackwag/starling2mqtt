FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

# Bridge code + deps (read-only at runtime).
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY starling2mqtt.py ./

# Runtime data: config.yaml (mounted) + published_entities.json (persisted).
WORKDIR /data

CMD ["python3", "/app/starling2mqtt.py"]
