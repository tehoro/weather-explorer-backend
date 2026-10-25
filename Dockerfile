# Weather display backend. Secrets come from the environment, never the image:
#   ANTHROPIC_API_KEY, APP_KEY
# Mount a volume at /app/data to keep tokens, the highlights feed and the download cache.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev
COPY . .
ENV SWEEP_HOURS_UTC=3,15
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
