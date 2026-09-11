# CI resolves this tag to a digest and passes PYTHON_IMAGE explicitly.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:24-bookworm-slim
FROM ${NODE_IMAGE} AS webbuild
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY pyproject.toml requirements.lock ./
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
RUN /venv/bin/pip install --no-cache-dir --no-deps .

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 hivemind && useradd --uid 10001 --gid 10001 --no-create-home hivemind
COPY --from=build /venv /venv
COPY --from=webbuild /web/dist /app/web/dist
WORKDIR /app
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=4s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"
CMD ["uvicorn", "hivemind.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "35"]
