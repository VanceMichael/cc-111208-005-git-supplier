FROM python:3.11-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN python -m scripts.materialize
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "scripts.demo"]
