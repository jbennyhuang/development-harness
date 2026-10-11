FROM debian:bookworm-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates unzip build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://github.com/jdx/mise/releases/download/v2026.9.18/mise-v2026.9.18-linux-x64.tar.gz \
    | tar -xz -C /usr/local/bin --strip-components=2 mise/bin/mise
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_PREFERENCE=only-managed \
    PATH="/opt/harness/bin:/root/.local/share/mise/installs/node/24.21.0/bin:/usr/local/bin:/usr/bin:/bin"
RUN uv python install 3.14.3 && uv venv --python 3.14.3 /opt/harness
RUN mise use --global node@24.21.0 && mise reshim
RUN mise exec node@24.21.0 -- npm install --global @agentclientprotocol/codex-acp@2.2.2
RUN mkdir /opt/agy && curl -fsSL \
    https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.3.0-linux-x86_64.zip \
    -o /tmp/agy.zip && unzip /tmp/agy.zip -d /opt/agy && chmod +x /opt/agy/agy_acp_server.par && rm /tmp/agy.zip
COPY requirements.txt /tmp/requirements.txt
RUN uv pip sync --python /opt/harness/bin/python /tmp/requirements.txt
COPY harness.whl /tmp/harness.whl
RUN uv pip install --python /opt/harness/bin/python --no-deps /tmp/harness.whl
WORKDIR /workspace
CMD ["sleep", "infinity"]
