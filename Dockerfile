# syntax=docker/dockerfile:1
# Pipeline + ops image. Build:  docker build -t retail-pipeline:local .
# NOT built or run on the authoring machine (no Docker there): linted with hadolint only.
# Digest-pin the base image (python:3.13.5-slim-bookworm@sha256:...) when you build for real; see platform/README.md. pipeline/ and data/ are vendored copies (docs/SPLIT.md).

FROM python:3.13.5-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY requirements-runtime.txt ./
RUN pip install -r requirements-runtime.txt
COPY pyproject.toml README.md LICENSE ./
COPY pipeline ./pipeline
COPY data ./data
COPY retail_ops ./retail_ops
RUN pip install --no-deps .

FROM python:3.13.5-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 app \
    && useradd --system --no-log-init --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir /work && chown app:app /work
COPY --from=build /opt/venv /opt/venv
WORKDIR /work
USER 10001:10001
# Batch job, not a service: no listening port and no HEALTHCHECK. Liveness of the install: `docker run IMAGE healthcheck`.
ENTRYPOINT ["python", "-m", "retail_ops"]
CMD ["task"]
