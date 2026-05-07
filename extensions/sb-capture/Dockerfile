FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir 'fastapi>=0.115' 'uvicorn[standard]>=0.30'

COPY main.py /app/main.py

# Default output path is mountable: bind a host volume to /var/lib/sb-capture.
ENV SB_CAPTURE_OUTPUT_PATH=/var/lib/sb-capture/events.ndjson
RUN mkdir -p /var/lib/sb-capture

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=2s --start-period=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
