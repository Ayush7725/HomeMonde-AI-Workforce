FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY workforce ./workforce
RUN pip install . && useradd --system --uid 10001 workforce

COPY alembic.ini ./
COPY migrations ./migrations

USER workforce
EXPOSE 8000
# Tenant packs are mounted at /app/tenants; secrets arrive as environment variables.
ENV WORKFORCE_TENANTS_DIR=/app/tenants
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["workforce", "serve"]
