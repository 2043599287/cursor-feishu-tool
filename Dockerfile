FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    CONTAINER_WORKSPACE=/workspace \
    HOST_WORKSPACE=/home/uisee/workspace \
    PATH=/home/uisee/.local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        python3-pip \
        python3-venv \
        git \
        bash \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 uisee \
    && useradd -m -u 1000 -g 1000 -s /bin/bash uisee \
    && echo "uisee ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/uisee \
    && mkdir -p /home/uisee/.local/bin /home/uisee/.local/share /home/uisee/.cursor \
    && chown -R uisee:uisee /home/uisee

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY --chown=uisee:uisee . /app
RUN mkdir -p /workspace /app/data \
    && chown -R uisee:uisee /app /workspace \
    && chmod +x /app/entrypoint.sh

USER uisee
WORKDIR /app

ENV CURSOR_AGENT_BIN=/home/uisee/.local/bin/agent \
    DATA_DIR=/app/data \
    SECRET_CONFIG_FILE=/app/secret_config.yaml \
    NO_OPEN_BROWSER=1

ENTRYPOINT ["/app/entrypoint.sh"]
