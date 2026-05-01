# ── Stage 1: build React ────────────────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app/web/client
COPY web/client/package*.json ./
RUN npm ci
COPY web/client/ ./
RUN npm run build

# ── Stage 2: Python app + built frontend ────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONUTF8=1

# System deps for psycopg2-binary and web3
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python package
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Copy backend web module
COPY web/server/ ./web/server/
COPY web/__init__.py ./web/__init__.py

# Copy built React from stage 1
COPY --from=frontend /app/web/client/dist ./web/client/dist

EXPOSE 8080
CMD ["polymarket", "web", "--host", "0.0.0.0", "--port", "8080"]
