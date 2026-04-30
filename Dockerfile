# syntax=docker/dockerfile:1.6
# Multi-stage Dockerfile: stage `build` clones mud at MUD_REF and compiles
# mud-sim; stage `runtime` runs the Flask UI with the prebuilt binary and a
# prepared world. We clone in the builder rather than COPY'ing the submodule
# from the host because git submodule pointers don't survive a plain COPY
# (cmake's changelog target then can't resolve `git rev-parse` on submodules).
ARG MUD_REPO=https://github.com/Bylins-Team/mud.git
ARG MUD_REF=claude/vibrant-raman-695d14

# ----------------------------------------------------------------------
# Stage 1: build mud-sim + prepare the small/ world
# ----------------------------------------------------------------------
FROM ubuntu:24.04 AS build

# Re-declare ARGs after FROM (per Docker spec they reset across stages).
ARG MUD_REPO
ARG MUD_REF

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git gettext python3 python3-pip python3-ruamel.yaml \
    libssl-dev libcurl4-gnutls-dev libexpat1-dev libgtest-dev libyaml-cpp-dev \
    zlib1g-dev libfmt-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --recurse-submodules --depth 1 -b "${MUD_REF}" "${MUD_REPO}" mud
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
# Same base as `build` stage so glibc / libstdc++ / libfmt / libyaml-cpp
# versions match the binary we copy in. Switching to python:slim would
# require pinning all of those pkgs to compatible versions on a different
# distro -- not worth the maintenance.
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    libyaml-cpp0.8 libfmt9 libcurl4t64 libexpat1 libssl3t64 zlib1g \
    python3 python3-pip python3-venv curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# pip install -e .[prod] needs venv on Ubuntu 24.04 (PEP 668).
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

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
    FLASK_APP=app \
    HOME=/tmp

# Ensure /data/runs is writable for any UID compose chooses. Without this
# the bind-mounted host dir would inherit container-root ownership; we
# override via compose `user: "1000:1000"` (or whatever the host UID is).
RUN mkdir -p /data/runs && chmod 0777 /data/runs

VOLUME ["/data/runs"]
EXPOSE 5001

CMD ["gunicorn", "-b", "0.0.0.0:5001", "--workers", "1", "--threads", "4", "--timeout", "180", "app:create_app()"]
