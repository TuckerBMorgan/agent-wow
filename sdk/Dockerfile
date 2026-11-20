# syntax=docker/dockerfile:1.7
FROM docker.io/acore/ac-wotlk-client-data@sha256:fc880ca1344cc1d588eb2144f85af820edd50b3d629adf05d3823d7554974441 AS data

FROM debian:bookworm-slim AS navigation
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl g++ \
    && rm -rf /var/lib/apt/lists/*
ARG AZEROTHCORE_REF=591f79586bbf04e5cdbb2a81bd98c5fd3b7192bd
RUN mkdir /source && curl -fsSL "https://codeload.github.com/azerothcore/azerothcore-wotlk/tar.gz/${AZEROTHCORE_REF}" \
    | tar -xz --strip-components=1 -C /source
COPY navigation.cpp /navigation.cpp
RUN g++ -std=c++17 -O2 -fPIC -shared -I/source/deps/recastnavigation/Detour/Include \
    /navigation.cpp /source/deps/recastnavigation/Detour/Source/*.cpp -o /navigation.so

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir grpcio-tools==1.78.0 grpcio-health-checking==1.78.0
WORKDIR /api
COPY --from=api module/v1/session.proto api/module/v1/session.proto
COPY module.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. \
    --include_imports --descriptor_set_out=module.pb module.proto api/module/v1/session.proto
COPY --from=navigation /navigation.so .
COPY --from=data /azerothcore/env/dist/data/dbc /data/dbc
COPY --from=data /azerothcore/env/dist/data/mmaps /data/mmaps
COPY wow_sdk ./wow_sdk
COPY module.py .
ENV PYTHONPATH=/api PYTHONUNBUFFERED=1
CMD ["python", "/api/module.py"]
