# syntax=docker/dockerfile:1.7
ARG GO_VERSION=1.27.2
FROM golang:${GO_VERSION}-bookworm AS builder
WORKDIR /src
ENV GODEBUG=http2client=0
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY main.go ./
COPY cmd ./cmd
COPY internal ./internal
COPY pkg ./pkg
COPY api ./api
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -o /out/agent-wow . \
    && GOBIN=/out go install google.golang.org/protobuf/cmd/protoc-gen-go \
    && GOBIN=/out go install google.golang.org/grpc/cmd/protoc-gen-go-grpc

FROM node:24-bookworm-slim
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN printf 'Acquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/99-no-pipeline \
    && apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq ripgrep python3 python3-venv build-essential \
      protobuf-compiler procps iproute2 iptables tini util-linux \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/* \
    && usermod -aG docker node
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir grpcio grpcio-tools protobuf requests pyyaml
COPY --from=builder /usr/local/go /usr/local/go
COPY --from=builder /out/ /usr/local/bin/
ARG WORKSPACE_REF=1c32e7e14265b6945ec3009c4ebbe8d4ae8bbc97
ADD https://github.com/TuckerBMorgan/agent-wow-workspace.git#${WORKSPACE_REF} /workspace/
COPY --from=builder /src /workspace/agent-wow
ARG AZEROTHCORE_REF=591f79586bbf04e5cdbb2a81bd98c5fd3b7192bd
RUN git init /opt/azerothcore-reference \
    && git -C /opt/azerothcore-reference remote add origin https://github.com/azerothcore/azerothcore-wotlk.git \
    && git -C /opt/azerothcore-reference fetch --depth 1 origin "${AZEROTHCORE_REF}" \
    && git -C /opt/azerothcore-reference checkout --detach FETCH_HEAD
COPY --chmod=755 benchmark/start-workspace.sh /usr/local/bin/start-workspace
COPY --chmod=755 benchmark/snapshot.sh /usr/local/bin/snapshot-characters
WORKDIR /workspace
RUN printf 'AGENT\nHARBORPLAYER\n' | agent-wow auth init \
    && chown -R node:node /workspace
RUN printf '%s\n' 'export PATH="/usr/local/go/bin:/opt/venv/bin:$HOME/go/bin:$PATH"' \
    > /etc/profile.d/agent-wow-path.sh
ENV PATH="/usr/local/go/bin:/opt/venv/bin:/home/node/go/bin:${PATH}" \
    DOCKER_HOST=unix:///var/run/docker.sock \
    AGENT_WOW_AUTHSERVER_HOST=authserver AGENT_WOW_AUTHSERVER_PORT=3724 \
    AGENT_WOW_WORLDSERVER_ADDRESS=worldserver:8085
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-workspace"]
CMD ["sleep", "infinity"]
