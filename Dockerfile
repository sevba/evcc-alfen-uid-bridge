FROM python:3.11-slim

RUN useradd -r -u 1000 -m bridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge/ bridge/
COPY main.py .

USER bridge

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os, sys; sys.exit(0 if os.path.exists('/tmp/bridge.healthy') else 1)"

CMD ["python3", "-u", "main.py"]
