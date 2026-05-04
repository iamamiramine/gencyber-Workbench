FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV CHALLENGE_ROOT=/workspace/challenges
ENV PYTHONPATH=/app/challenge_api/src
ENV PORT=3000

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    supervisor \
    git \
  && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY challenge_api/requirements.txt /app/challenge_api/requirements.txt
RUN pip3 install --no-cache-dir -r /app/challenge_api/requirements.txt

COPY challenge_api/src /app/challenge_api/src
COPY terminal_session /app/terminal_session
# node-pty native addon: prebuilds may be missing; node-gyp needs make/g++ (see gencyber-dev terminal-session on node:18)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
  && cd /app/terminal_session && npm ci --omit=dev \
  && apt-get purge -y build-essential \
  && apt-get autoremove -y \
  && rm -rf /var/lib/apt/lists/*

COPY supervisord.conf /etc/supervisor/conf.d/workbench.conf

RUN mkdir -p /workspace/challenges /var/log/supervisor

EXPOSE 80 3000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
