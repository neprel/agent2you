# The agent image: one agent = one container, built once, instantiated per agent.
#
# The stack inside, bottom to top:
#
#   claude / codex / opencode / cline   the brain and the hands. Each spends a
#                                       SUBSCRIPTION through its own CLI login --
#                                       no provider API key exists anywhere in a
#                                       default deployment, which is the legal
#                                       basis of the arrangement.
#   acp2api                             one OpenAI-compatible endpoint per CLI,
#                                       over ACP. It spawns the CLI as a child.
#   litellm                             the failover chain -- the only layer that
#                                       can decide to try the next brain on 429.
#   hermes                              the employee: chat presence, sessions,
#                                       memory, cron. Speaks OpenAI and nothing
#                                       else, which is why the layers below exist.
#
# Everything is baked in and pinned: every instance is byte-identical and a
# rollback is an image-tag edit. What is NOT in here: the logins. ~/.claude,
# ~/.codex and friends are volumes, so a rebuild never signs an agent out.
#
# Build ON THE HOST THAT RUNS IT (`a2y build`): native npm/pip packages make a
# cross-arch build a qemu hour.
#
# Extending: this file is vendored into your fleet workspace -- edit it, or build
# a derived image `FROM` this one, and point fleet.yaml `image.tag` at the result.

# Node from the official image, not a distro repo: current runtimes for the five
# npm packages. bookworm on purpose -- its binaries link glibc 2.36, which runs
# on noble's 2.39; the reverse would not.
FROM node:24-bookworm-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS node_source

# Hermes lazy-installs optional plugin dependencies with uv at runtime; without
# uv on PATH those installs fail the first time a feature is used.
FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv_source

FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

# Pinned in the image, not in .env: versions are not secrets, and a tracked pin
# is the only reason two agents built a month apart run the same software.
ARG CLAUDE_CODE_VERSION=2.1.226
ARG CODEX_VERSION=0.147.0
ARG OPENCODE_VERSION=1.18.16
# cline is pinned AND self-updates from `latest` at startup with
# --min-release-age=0 unless CLINE_NO_AUTO_UPDATE=1 is set in the container
# environment (the pack sets it). Without that variable this pin is fiction.
ARG CLINE_VERSION=3.0.52
ARG ACP2API_VERSION=1.11.0
# Mattermost Boards/Playbooks as tools -- stdio SIDECARS reusing the container's
# own MATTERMOST_TOKEN, so an agent's reach is exactly its account's reach.
# Harmless when the fleet runs another platform; they are simply not configured.
ARG BOARDS_MCP_VERSION=1.0.0
ARG PLAYBOOKS_MCP_VERSION=1.0.0
# Phoenix over MCP: the fleet's own traces as tools.
ARG PHOENIX_MCP_VERSION=4.3.3
# OpenTelemetry plugin for Hermes, vendored at a TAG (`hermes plugins install`
# resolves to whatever main is that day).
ARG HERMES_OTEL_VERSION=0.11.0
# The hint CLI + hintbook: repositories that keep durable knowledge in .hint
# files expect agents to run `hint <path>` before touching anything.
ARG HINT_VERSION=1.2.0
ARG HINTBOOK_VERSION=1.1.1
# Forge CLIs, pinned tarballs with verified checksums.
ARG GH_VERSION=2.97.0
ARG TEA_VERSION=0.15.1
ARG LITELLM_VERSION=1.96.0
ARG HINDSIGHT_CLIENT_VERSION=0.9.0
# Hermes' PDF / legacy-Office reader is a LAZY dependency ("tool.doc_extract"),
# not an extra: without it baked in, the FIRST time an agent reads a
# .pdf/.docx/.xlsx attachment Hermes pip-installs it MID-TURN, unpinned, into
# the running container -- latency plus an unpinned install in a container full
# of OAuth logins. Baked and pinned instead.
ARG FIRECRAWL_ANYDOC_VERSION=0.1.6
# litellm 1.96.0 imports a symbol FastAPI removed in 0.141 and does not cap the
# dependency; installed AFTERWARDS on purpose (together it is ResolutionImpossible,
# after the fact pip downgrades and merely warns). Raise only after checking
# `fastapi.dependencies.utils.get_flat_dependant` still exists.
ARG FASTAPI_VERSION=0.135.0
# Both spec-driven suites, so an agent can be handed work in either form.
ARG SPECIFY_VERSION=0.16.4
ARG OPENSPEC_VERSION=1.9.0
ARG YARN_VERSION=4.18.0
# The pack's own CLI, inside every agent: a supervisor agent edits manifests,
# runs `a2y agent add` / `a2y render` and commits -- inside its container,
# against the fleet workspace cloned into /work. Building images and starting
# containers stay OUTSIDE (no docker socket in here, ever); the supervisor
# prepares, the operator or CI executes.
ARG AGENT2YOU_VERSION=1.3.2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/agent/hermes-agent/venv/bin:/opt/agent/litellm/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin

# ca-certificates is load-bearing: codex login is a Rust binary using the SYSTEM
# trust store while Node uses bundled roots -- on an image without a CA store
# everything Node does works and every non-Node tool reports what reads like a
# network fault. git/ssh/ripgrep/build-essential: this container exists to run
# coding agents against a checkout.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client ripgrep jq less procps \
        python3 python3-venv python3-dev build-essential supervisor tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_source /usr/local /usr/local
COPY --from=uv_source /uv /usr/local/bin/uv

# gh (GitHub) and tea (Gitea), pinned and checksum-verified against the
# publisher's own sums. `dpkg --print-architecture` rather than hardcoded amd64:
# a wrong-arch binary fails with `exec format error`, which names nothing.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    cd /tmp; \
    curl -fsSL -o gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSL -o gh.sums "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"; \
    grep " gh_${GH_VERSION}_linux_${arch}.tar.gz\$" gh.sums | sed "s| .*| gh.tgz|" | sha256sum -c -; \
    tar -xzf gh.tgz; \
    install -m 0755 "gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    curl -fsSL -o tea "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${arch}"; \
    curl -fsSL -o tea.sha256 "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${arch}.sha256"; \
    sed "s| .*| tea|" tea.sha256 | sha256sum -c -; \
    install -m 0755 tea /usr/local/bin/tea; \
    rm -rf /tmp/gh.tgz /tmp/gh.sums /tmp/gh_* /tmp/tea /tmp/tea.sha256; \
    gh --version; tea --version | head -1

# npm global prefix is the image's /usr/local, so these land in a layer, not a
# volume. `cline`, NOT `@cline/cli` (the latter installs an experimental `clite`).
# `--include=optional` is not belt-and-braces: the Claude Agent SDK ships its
# actual binary as a platform-specific optionalDependency, and one build without
# it fails every request with a content-free `-32603 Internal error`.
RUN npm install -g --include=optional --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
        "cline@${CLINE_VERSION}" \
        "acp2api@${ACP2API_VERSION}" \
        "mattermost-boards-mcp@${BOARDS_MCP_VERSION}" \
        "mattermost-playbooks-mcp@${PLAYBOOKS_MCP_VERSION}" \
        "@arizeai/phoenix-mcp@${PHOENIX_MCP_VERSION}" \
        "@openhint/cli@${HINT_VERSION}" \
        "@openhint/hintbook-software-engineer@${HINTBOOK_VERSION}" \
        "@fission-ai/openspec@${OPENSPEC_VERSION}" \
    && npm cache clean --force

# Spec Kit via `uv tool install` (its own instruction; isolated env, entry point
# in /root/.local/bin). The two `rm`s clear DANGLING yarn symlinks: node's image
# links /usr/local/bin/yarn into /opt, which the COPY above does not carry, and
# corepack dies on the corpse with an ENOENT naming node's fs promises. corepack
# then makes yarn 4 the default for repositories that declare nothing.
RUN rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && uv tool install --no-cache "specify-cli==${SPECIFY_VERSION}" \
    && uv tool install --no-cache "agent2you==${AGENT2YOU_VERSION}" \
    && a2y --version \
    && corepack enable \
    && corepack prepare "yarn@${YARN_VERSION}" --activate \
    && specify --help >/dev/null \
    && openspec --version >/dev/null

# Fail the BUILD rather than ship an agent that cannot answer: verify the Claude
# Agent SDK's platform binary actually landed (matched by glob -- the package
# name is per platform).
RUN set -eu; \
    dir=$(find /usr/local/lib/node_modules -maxdepth 4 -type d -name 'claude-agent-sdk-*' | head -1); \
    [ -n "$dir" ] && [ -x "$dir/claude" ] \
      || { echo "FATAL: the Claude Agent SDK binary is missing from this image."; \
           echo "Its optionalDependency was skipped; every request would fail with"; \
           echo "a content-free -32603 Internal error."; \
           exit 1; }; \
    echo "Claude Agent SDK binary present: $dir/claude"

# Hermes by its OWN installer (pip-from-PyPI lags and misses plugins; hand-rolled
# pip-from-git means maintaining someone else's install logic).
#   --dir          code OUTSIDE /root/.hermes, which is a volume at runtime
#   --hermes-home  state where the volume will be
# This tracks main; the installer takes `--commit <sha>` if reproducibility ever
# matters more than being current.
RUN curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh -o /tmp/install.sh \
    && bash /tmp/install.sh \
        --non-interactive --skip-setup --skip-browser \
        --dir /opt/agent/hermes-agent \
        --hermes-home /root/.hermes \
    && rm -f /tmp/install.sh \
    && uv pip install --no-cache --python /opt/agent/hermes-agent/venv/bin/python \
        "hindsight-client==${HINDSIGHT_CLIENT_VERSION}" \
        "firecrawl-anydoc==${FIRECRAWL_ANYDOC_VERSION}" \
    # Refuse Hermes' own self-update. detect_install_method() reads
    # <install-tree>/.install_method FIRST and honours it as authoritative; a
    # `docker` stamp makes every update path decline. Without it, an agent (or
    # the dashboard's update button) can replace pinned code in a running
    # container -- the exact failure mode cline's self-update already
    # demonstrated in this image's history.
    && printf 'docker\n' > /opt/agent/hermes-agent/.install_method

# The OTel plugin, and its python dependencies BY HAND: plugin.yaml cannot
# declare them, and a plugin whose imports fail is logged once at startup and
# then silently absent -- the fleet runs untraced and looks fine.
# Directory named for the manifest's `name:` (hermes_otel), not the repository.
RUN git clone --depth 1 --branch "hermes-otel-v${HERMES_OTEL_VERSION}" \
        https://github.com/briancaffey/hermes-otel /opt/agent/plugins/hermes_otel \
    && rm -rf /opt/agent/plugins/hermes_otel/.git \
              /opt/agent/plugins/hermes_otel/tests \
              /opt/agent/plugins/hermes_otel/website \
              /opt/agent/plugins/hermes_otel/video \
    && uv pip install --no-cache --python /opt/agent/hermes-agent/venv/bin/python \
        opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http

# litellm in a virtualenv of its own: Hermes exact-pins its whole tree as a
# supply-chain measure and litellm pins ranges over the same packages; one
# environment means the resolver fails or silently mismatches one of them.
RUN python3 -m venv /opt/agent/litellm \
    && /opt/agent/litellm/bin/pip install --no-cache-dir \
        "litellm[proxy]==${LITELLM_VERSION}" \
    && /opt/agent/litellm/bin/pip install --no-cache-dir \
        "fastapi==${FASTAPI_VERSION}"

COPY plugins /opt/agent/plugins
COPY entrypoint.sh /opt/agent/bin/entrypoint.sh
# Runs twice: once from the entrypoint, then on a loop under supervisord, so an
# agent added to the fleet is discovered without restarting the others.
COPY fleet-roster.py /opt/agent/bin/fleet-roster.py
# Pushes each bank's mission into Hindsight, which the Hermes plugin reads from
# config and never applies itself.
COPY apply-memory-profile.py /opt/agent/bin/apply-memory-profile.py
# Per-program sections, assembled by the entrypoint: hermes and fleet-roster
# always; acp2api and litellm only when their config was rendered for this
# agent. An agent pointed straight at an OpenAI endpoint runs neither.
COPY supervisord.d /opt/agent/supervisord.d
COPY bashrc /root/.bashrc
RUN chmod +x /opt/agent/bin/entrypoint.sh /opt/agent/bin/fleet-roster.py \
        /opt/agent/bin/apply-memory-profile.py \
    && printf '[ -f /root/.bashrc ] && . /root/.bashrc\n' > /root/.bash_profile

# Where the agent works. A volume in every deployment; present in the image so a
# bare `docker run` smoke test still has somewhere to stand.
RUN mkdir -p /work /root/.hermes
WORKDIR /work

# tini reaps the zombies that the coding CLIs, their ACP children and every MCP
# stdio subprocess leave behind; supervisord does not reap what it did not spawn.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agent/bin/entrypoint.sh"]
