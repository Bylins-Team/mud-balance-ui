# syntax=docker/dockerfile:1.6
# Multi-stage Dockerfile: stage `build` compiles mud-sim from the bylins/mud
# submodule, stage `runtime` runs the Flask UI with the prebuilt binary and
# a prepared world.
ARG MUD_SUBMODULE_PATH=mud

# ----------------------------------------------------------------------
# Stage 1: build mud-sim + prepare the small/ world
# ----------------------------------------------------------------------
FROM ubuntu:24.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git gettext python3 python3-pip python3-ruamel.yaml \
    libssl-dev libcurl4-gnutls-dev libexpat1-dev libgtest-dev libyaml-cpp-dev \
    zlib1g-dev libfmt-dev libnlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY ${MUD_SUBMODULE_PATH} /src/mud
WORKDIR /src/mud

# Build mud-sim with YAML support (required for the simulator).
RUN cmake -B build_yaml -S . \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTS=OFF \
        -DHAVE_YAML=ON \
    && cmake --build build_yaml --target mud-sim -j"$(nproc)"

# Prepare a default world (small/), convert it to YAML format.
RUN mkdir -p /opt/small && \
    cp -r lib/* /opt/small/ && \
    cp -r lib.template/* /opt/small/ && \
    python3 tools/converter/convert_to_yaml.py -i /opt/small -o /opt/small -f yaml

# ----------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libyaml-cpp0.8 libfmt9 libcurl4 libexpat1 libssl3 zlib1g curl \
    && rm -rf /var/lib/apt/lists/*

# mud-sim binary + default world from stage build.
COPY --from=build /src/mud/build_yaml/mud-sim /opt/mud-sim
COPY --from=build /opt/small /opt/small

WORKDIR /app
COPY pyproject.toml /app/
COPY app /app/app
COPY templates /app/templates
COPY static /app/static

# Vendor htmx (not committed to the repo to keep it lean).
RUN curl -fsSL -o /app/static/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js

RUN pip install --no-cache-dir -e ".[prod]"

ENV MUD_SIM_BIN=/opt/mud-sim \
    MUD_SIM_WORLD_DIR=/opt/small \
    RUNS_DIR=/data/runs \
    MUD_SIM_TIMEOUT_S=120 \
    FLASK_APP=app

VOLUME ["/data/runs"]
EXPOSE 5001

CMD ["gunicorn", "-b", "0.0.0.0:5001", "--workers", "1", "--threads", "4", "--timeout", "180", "app:create_app()"]
