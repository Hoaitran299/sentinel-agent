# Runner cho watcher + dashboard. Worktree Laravel được bootstrap/verify ngay trong
# container nên image cần đủ toolchain: PHP 8.3 + Composer, Node 22, Git, Claude Code CLI.
FROM node:22-bookworm-slim AS node

FROM php:8.3-cli-bookworm

COPY --from=mlocati/php-extension-installer:2 /usr/bin/install-php-extensions /usr/local/bin/
RUN install-php-extensions pdo_mysql zip intl bcmath pcntl \
    && apt-get update \
    && apt-get install -y --no-install-recommends git unzip openssh-client ca-certificates \
       python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code \
    && git config --system --add safe.directory '*'

ARG INSTALL_S3=false
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH=$VIRTUAL_ENV/bin:$PATH
COPY pyproject.toml /tmp/build/
COPY src /tmp/build/src
RUN pip install -q --no-cache-dir "/tmp/build$( [ "$INSTALL_S3" = "true" ] && echo '[s3]' )" \
    && rm -rf /tmp/build

RUN useradd --create-home --uid 1000 --shell /bin/bash aifix \
    && mkdir -p /var/lib/ai-fix/worktrees \
    && chown -R aifix:aifix /var/lib/ai-fix /home/aifix

USER aifix
WORKDIR /app
# Source được bind-mount vào /app; PYTHONPATH ưu tiên code mount hơn bản cài trong venv.
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    HOME=/home/aifix
EXPOSE 8787
CMD ["python", "-m", "autofix_agent", "watch", "--config", "config/docker.json"]
