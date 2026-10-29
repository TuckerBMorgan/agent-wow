# syntax=docker/dockerfile:1.7

# Build agent-wow from an exact repository revision.
ARG GO_VERSION=1.27.2
FROM golang:${GO_VERSION}-bookworm AS agent-wow-builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG AGENT_WOW_REPOSITORY=https://github.com/TuckerBMorgan/agent-wow.git
ARG AGENT_WOW_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src \
    && git -C /src remote add origin "${AGENT_WOW_REPOSITORY}" \
    && git -C /src fetch --depth 1 origin "${AGENT_WOW_REF}" \
    && git -C /src checkout --detach FETCH_HEAD

WORKDIR /src
RUN go mod download \
    && mkdir -p /out \
    && go build -trimpath -o /out/agent-wow . \
    && GOBIN=/out go install google.golang.org/protobuf/cmd/protoc-gen-go \
    && GOBIN=/out go install google.golang.org/grpc/cmd/protoc-gen-go-grpc

# Start the benchmark image with Node for both subscription CLIs.
FROM node:24-bookworm-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
ARG CODEX_VERSION=0.163.0
ARG CLAUDE_VERSION=2.1.306
ARG AZEROTHCORE_REF=591f79586bbf04e5cdbb2a81bd98c5fd3b7192bd
LABEL org.opencontainers.image.azerothcore.revision="${AZEROTHCORE_REF}"

ENV PATH="/usr/local/go/bin:/opt/venv/bin:/root/go/bin:/home/node/go/bin:${PATH}" \
    DOCKER_HOST=unix:///var/run/docker.sock \
    AGENT_WOW_CONFIG_DIR=/workspace/config \
    AGENT_WOW_DATA_DIR=/workspace/data \
    AGENT_WOW_MODULE_DIR=/workspace/modules \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/workspace/tmp

# Install the runner, development tools, and nested Docker.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gnupg jq ripgrep \
      build-essential protobuf-compiler \
      python3 python3-venv \
      procps iproute2 iptables tini util-linux bubblewrap skopeo \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         docker-ce docker-ce-cli containerd.io docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install pinned Codex and Claude subscription clients.
RUN npm install --global \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
         boto3 docker grpcio grpcio-tools protobuf PyYAML requests \
    && install -d -m 0700 -o node -g node \
         /home/node/.codex /home/node/.claude /home/node/.cache /home/node/go

# The trusted supervisor remains root so it can manage nested Docker.
# The agent user is deliberately not added to the Docker group.

# Keep Go and the source tree available to the coding agent.
COPY --from=agent-wow-builder /usr/local/go /usr/local/go
COPY --from=agent-wow-builder /out/agent-wow /usr/local/bin/agent-wow
COPY --from=agent-wow-builder /out/protoc-gen-go /usr/local/bin/protoc-gen-go
COPY --from=agent-wow-builder /out/protoc-gen-go-grpc /usr/local/bin/protoc-gen-go-grpc
COPY --from=agent-wow-builder /src /opt/agent-wow

# Add the module template, workspace instructions, and server source.
RUN git clone --depth 1 https://github.com/agent-wow/go-module-template.git \
         /opt/go-module-template \
    && git clone --depth 1 https://github.com/TuckerBMorgan/agent-wow-workspace.git \
         /opt/workspace-template \
    && git init /opt/azerothcore-reference \
    && git -C /opt/azerothcore-reference remote add origin \
         https://github.com/azerothcore/azerothcore-wotlk.git \
    && git -C /opt/azerothcore-reference fetch --depth 1 origin "${AZEROTHCORE_REF}" \
    && git -C /opt/azerothcore-reference checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/azerothcore-reference rev-parse HEAD)" = "${AZEROTHCORE_REF}" \
    && mkdir -p /opt/server /opt/server-images /workspace

# Cache only reviewed digest-addressed images. AzerothCore's official CI built
# the four AzerothCore images below from AZEROTHCORE_REF.
ARG MYSQL_IMAGE=docker.io/library/mysql@sha256:a0225d1ea6631c8bc2385dc4c2faba17bbf1834ecf369e1659a9a382bd8d5dd7
ARG ACORE_DB_IMAGE=docker.io/acore/ac-wotlk-db-import@sha256:46856838c54cabfff110f532d587085613f4ef03d8515e7dd3870b037c0291b2
ARG ACORE_DATA_IMAGE=docker.io/acore/ac-wotlk-client-data@sha256:fc880ca1344cc1d588eb2144f85af820edd50b3d629adf05d3823d7554974441
ARG ACORE_AUTH_IMAGE=docker.io/acore/ac-wotlk-authserver@sha256:a0a656c9864a7d4c9de2680e3e9cc26b7b27d559a67accc4969faa96345ca64e
ARG ACORE_WORLD_IMAGE=docker.io/acore/ac-wotlk-worldserver@sha256:6186b9efbbef9efc7345ae8a067f6045f5462883c4df58a2d5785af7c514a8f4

RUN for image in \
      "${MYSQL_IMAGE}" \
      "${ACORE_DB_IMAGE}" \
      "${ACORE_DATA_IMAGE}" \
      "${ACORE_AUTH_IMAGE}" \
      "${ACORE_WORLD_IMAGE}"; do \
        case "${image}" in \
          *@sha256:*) ;; \
          *) echo "server image must use an immutable digest: ${image}" >&2; exit 1 ;; \
        esac; \
    done \
    && skopeo copy --override-arch amd64 \
      "docker://${MYSQL_IMAGE}" \
      "docker-archive:/opt/server-images/mysql.tar:mysql:pinned" \
    && skopeo copy --override-arch amd64 \
      "docker://${ACORE_DB_IMAGE}" \
      "docker-archive:/opt/server-images/db-import.tar:acore/ac-wotlk-db-import:pinned" \
    && skopeo copy --override-arch amd64 \
      "docker://${ACORE_DATA_IMAGE}" \
      "docker-archive:/opt/server-images/client-data.tar:acore/ac-wotlk-client-data:pinned" \
    && skopeo copy --override-arch amd64 \
      "docker://${ACORE_AUTH_IMAGE}" \
      "docker-archive:/opt/server-images/authserver.tar:acore/ac-wotlk-authserver:pinned" \
    && skopeo copy --override-arch amd64 \
      "docker://${ACORE_WORLD_IMAGE}" \
      "docker-archive:/opt/server-images/worldserver.tar:acore/ac-wotlk-worldserver:pinned"

# Copy the v3 Compose file, account helper, and single runner.
COPY docker/compose.yaml /opt/server/compose.yaml
COPY --chmod=755 docker/prepare_wow_account.py /usr/local/bin/
COPY --chmod=755 docker/prepare_viewer_account.py /usr/local/bin/
COPY --chmod=644 docker/control_plane.py /usr/local/bin/control_plane.py
COPY --chmod=755 docker/runner.py /usr/local/bin/runner.py

# Keep the toolchain on login-shell paths used by the agents.
RUN printf '%s\\n' \
      'export PATH="/usr/local/go/bin:/opt/venv/bin:$HOME/go/bin:$PATH"' \
      > /etc/profile.d/agent-wow-path.sh

WORKDIR /workspace

# Batch starts one supervisor and lets it own cleanup and reporting.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/runner.py"]
