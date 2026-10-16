FROM python:3.11.13-slim-bookworm@sha256:cec9aa7aa96eea4fa036e9b82be1e6b325f2e3707f462d885868df51ec0a4b47
RUN python -m pip install --no-cache-dir pytest==8.4.1
WORKDIR /workspace
