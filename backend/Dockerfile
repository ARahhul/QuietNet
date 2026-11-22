# QuietNet engine + API for the cloud VM (infra/docker-compose.cloud.yml builds this).
# Dependencies come from poetry.lock, so the VM runs exactly what the tests ran against.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app
RUN pip install "poetry>=2,<3"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root

COPY pahra ./pahra
COPY devtools ./devtools
RUN poetry install --only main

# Unprivileged user; /data holds pahra.db and the list cache (a named volume in compose).
RUN useradd --system --create-home --uid 10001 quietnet && mkdir /data && chown quietnet /data
USER quietnet
ENV DB_PATH=/data/pahra.db \
    LISTS_CACHE_DIR=/data/lists

EXPOSE 8000
# Caddy terminates TLS in front of this and sets X-Forwarded-For.
CMD ["uvicorn", "pahra.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
