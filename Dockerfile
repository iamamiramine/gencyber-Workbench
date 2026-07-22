# Kali-class CTF sandbox (curated tools on Debian) + challenge API + terminal session.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV CHALLENGE_ROOT=/workspace/challenges
ENV PYTHONPATH=/app/challenge_api/src
ENV PORT=3000
ENV GENCYBER_SANDBOX_PROFILE=kali-ctf

# Core runtime + CTF tool surface (aligned with ctf-skills prerequisites).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    docker.io \
    file \
    binutils \
    gdb \
    strace \
    ltrace \
    bsdextrautils \
    xxd \
    binwalk \
    foremost \
    tcpdump \
    netcat-openbsd \
    nmap \
    dnsutils \
    iputils-ping \
    bind9-host \
    whois \
    jq \
    unzip \
    php-cli \
    ruby \
    gcc \
    make \
    libgmp-dev \
    steghide \
    exiftool \
    openssh-client \
    sshpass \
    expect \
  && mkdir -p /usr/libexec/docker/cli-plugins \
  && case "$(dpkg --print-architecture)" in \
       amd64) COMPOSE_DL_ARCH=x86_64 ;; \
       arm64) COMPOSE_DL_ARCH=aarch64 ;; \
       *) echo "unsupported architecture for docker compose" >&2; exit 1 ;; \
     esac \
  && curl -fsSL "https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-${COMPOSE_DL_ARCH}" \
       -o /usr/libexec/docker/cli-plugins/docker-compose \
  && chmod +x /usr/libexec/docker/cli-plugins/docker-compose \
  && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && pip3 install --no-cache-dir \
    pycryptodome \
    requests \
    z3-solver \
    sympy \
    pwntools \
    setproctitle \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY challenge_api/requirements.txt /app/challenge_api/requirements.txt
RUN pip3 install --no-cache-dir -r /app/challenge_api/requirements.txt

COPY challenge_api/src /app/challenge_api/src
COPY terminal_session /app/terminal_session

# EnIGMA Interactive Agent Tools: make debug_*/connect_* available in every
# workbench shell, and keep pwntools quiet on a non-terminal stdout so the
# netcat REPL prompt is detectable by the interactive bridge.
RUN echo 'source /app/terminal_session/enigma_commands/init.sh 2>/dev/null' >> /root/.bashrc
ENV PWNLIB_NOTERM=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
  && cd /app/terminal_session && npm ci --omit=dev \
  && apt-get purge -y build-essential \
  && apt-get autoremove -y \
  && rm -rf /var/lib/apt/lists/*

COPY supervisord.conf /etc/supervisor/conf.d/workbench.conf

RUN mkdir -p /workspace/challenges /var/log/supervisor /bottom \
  && ln -sfn /workspace /bottom

EXPOSE 80 3000

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
