FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HELIX_DATA_DIR=/data

WORKDIR /app
RUN groupadd --gid 10001 helix \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin helix \
    && install -d -m 0700 -o 10001 -g 10001 /data

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 10001:10001
EXPOSE 8080
CMD ["helix-mcp", "serve", "--host", "0.0.0.0", "--port", "8080"]
