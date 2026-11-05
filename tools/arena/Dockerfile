FROM python:3.12-slim
RUN pip install --no-cache-dir grpcio-tools==1.78.0 grpcio-health-checking==1.78.0
WORKDIR /api
COPY api/module/v1/session.proto api/module/v1/session.proto
COPY tools/arena/arena.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. \
    --include_imports --descriptor_set_out=module.pb arena.proto api/module/v1/session.proto
ENV PYTHONPATH=/api PYTHONUNBUFFERED=1
WORKDIR /fighter
CMD ["python", "arena.py"]
