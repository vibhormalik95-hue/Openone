# Disposable proof runner; all application/database calls execute as UID 10001.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-dev.lock \
    && groupadd --gid 10001 hivemind \
    && useradd --uid 10001 --gid 10001 --no-create-home hivemind \
    && mkdir /evidence && chown 10001:10001 /evidence
COPY pyproject.toml requirements.lock requirements-dev.lock ./
COPY src/ ./src/
COPY sql/ ./sql/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY deploy/proof_entrypoint.py ./deploy/proof_entrypoint.py
USER 10001:10001
ENTRYPOINT ["python", "deploy/proof_entrypoint.py"]
