FROM python:3.12-slim
RUN pip install --no-cache-dir grpcio-tools==1.78.0 grpcio-health-checking==1.78.0
WORKDIR /api
COPY --from=api module/v1/session.proto api/module/v1/session.proto
COPY connector.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. \
    --include_imports --descriptor_set_out=module.pb connector.proto api/module/v1/session.proto
COPY connector.py runner.py state.py ./
ENV PYTHONPATH=/api PYTHONUNBUFFERED=1
WORKDIR /harness
CMD ["python", "/api/connector.py"]
