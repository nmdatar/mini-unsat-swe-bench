# syntax=docker/dockerfile:1.7

ARG RUST_IMAGE=rust:1.97.0-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.4

FROM ${UV_IMAGE} AS uv
FROM ${RUST_IMAGE}

ARG RUFF_REPOSITORY=https://github.com/astral-sh/ruff.git
ARG RUFF_COMMIT=main
ARG PRECOMPILE=true

ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_HOME=/usr/local/cargo \
    CARGO_INCREMENTAL=1 \
    CARGO_TERM_COLOR=never \
    RUST_BACKTRACE=1 \
    UV_CACHE_DIR=/opt/uv-cache

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        libssl-dev \
        patch \
        pkg-config \
        python3 \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
COPY setup.sh /usr/local/bin/benchmark-setup

RUN chmod 0755 /usr/local/bin/benchmark-setup \
    && mkdir -p /testbed /opt/uv-cache \
    && git init /testbed \
    && git -C /testbed remote add origin "${RUFF_REPOSITORY}" \
    && git -C /testbed fetch --depth=1 origin "${RUFF_COMMIT}" \
    && git -C /testbed checkout --detach FETCH_HEAD \
    && git -C /testbed remote remove origin \
    && git -C /testbed config user.name "Benchmark Agent" \
    && git -C /testbed config user.email "benchmark-agent@localhost"

WORKDIR /testbed

# rustup reads the task revision's rust-toolchain.toml, so historical tasks can
# select their own pinned compiler even though the base image has a default.
RUN rustup show active-toolchain \
    && cargo fetch --locked \
    && if [ "${PRECOMPILE}" = "true" ]; then \
        cargo test --locked --package ruff --no-run \
        && cargo test --locked --package ruff_linter --no-run \
        && cargo test --locked --package ruff_python_formatter --no-run \
        && cargo test --locked --package ruff_python_parser --no-run; \
    fi \
    && git status --short

# Runtime containers are network-isolated. Offline mode also makes missing
# cached dependencies fail immediately instead of waiting on connection retries.
ENV CARGO_NET_OFFLINE=true \
    UV_OFFLINE=true

ENTRYPOINT ["/usr/local/bin/benchmark-setup"]
CMD ["bash"]
