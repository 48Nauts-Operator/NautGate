FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "asyncpg>=0.29"

COPY tsb.py main.py migrate.py /app/

EXPOSE 8004
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8004/health').read()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]
