FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir 'fastapi>=0.115' 'uvicorn[standard]>=0.30' 'asyncpg>=0.29' 'pyyaml>=6.0'

COPY chain.py main.py migrate.py allowlist.yaml /app/

EXPOSE 8003

HEALTHCHECK --interval=10s --timeout=2s --start-period=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8003/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
