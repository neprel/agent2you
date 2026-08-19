#!/bin/bash
# Entrypoint for an agent container. Everything here is preflight: by the time
# supervisord starts, the network is proven, the identity is on disk, and the
# secrets are in the one file Hermes reads them from.
set -eo pipefail

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${AGENT_NAME:-agent}: $1"; }
die() { log "ERROR: $1 (step: $2)"; exit 1; }

: "${AGENT_CONFIG_DIR:=/config}"
: "${HERMES_HOME:=/root/.hermes}"

log "Step 0: Reading ${AGENT_CONFIG_DIR}/agent.yaml"
# Everything about an agent that is not a secret lives in this one generated
# file. Values already present in the environment WIN, so compose can override
# any of it for a one-off without re-rendering.
#
# Read with the hermes venv's python: the only interpreter here with PyYAML, and
# a shell parser for YAML is how a config gets subtly wrong.
if [ -f "${AGENT_CONFIG_DIR}/agent.yaml" ]; then
    eval "$(/opt/agent/hermes-agent/venv/bin/python3 - "${AGENT_CONFIG_DIR}/agent.yaml" <<'PY'
import sys, shlex, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
p, mm, mem = c.get("ports") or {}, c.get("mattermost") or {}, c.get("memory") or {}
out = {
    "AGENT_NAME": c.get("name"),
    "AGENT_DESCRIPTION": (c.get("description") or "").strip(),
    "ACP2API_PORT": p.get("acp2api"),
    "LITELLM_PORT": p.get("litellm"),
    "A2A_PORT": p.get("a2a"),
    # The Agent Card is built from the same fields the roster reads, so peers
    # never describe this agent by hand.
    "A2A_AGENT_NAME": c.get("name"),
    "A2A_AGENT_DESCRIPTION": (c.get("description") or "").strip(),
    "MATTERMOST_REPLY_MODE": mm.get("reply_mode"),
    "MATTERMOST_REQUIRE_MENTION": mm.get("require_mention"),
    "HINDSIGHT_MODE": mem.get("mode"),
    "HINDSIGHT_API_URL": mem.get("url"),
}
if p.get("acp2api"):
    out["ACP2API_BASE_URL"] = f"http://127.0.0.1:{p['acp2api']}/v1"
if p.get("litellm"):
    out["LITELLM_BASE_URL"] = f"http://127.0.0.1:{p['litellm']}/v1"
for k, v in out.items():
    if v is None or v == "":
        continue
    if isinstance(v, bool):
        v = "true" if v else "false"
    print(f"export {k}=${{{k}:-{shlex.quote(str(v))}}}")
PY
)"
    # The healthcheck runs as a fresh exec and inherits compose's environment,
    # not ours -- so the resolved values are written where it can source them.
    { echo "LITELLM_PORT=${LITELLM_PORT}"; echo "ACP2API_PORT=${ACP2API_PORT}"; } > /run/agent.env
    log "  ${AGENT_NAME}: acp2api :${ACP2API_PORT}, litellm :${LITELLM_PORT}"
else
    log "  no agent.yaml -- relying entirely on the environment"
fi

: "${AGENT_NAME:?AGENT_NAME is not set and ${AGENT_CONFIG_DIR}/agent.yaml did not supply it}"

log "Step 0a: Deciding where acp2api serves its metrics"
# Loopback-bound everywhere else is right -- the API has no auth and spends a
# subscription. Metrics are the one thing reached from OUTSIDE:
#   * bridge network: A2Y_METRICS_BIND (e.g. 0.0.0.0 -- the fleet network plus
#     whatever is attached, nothing more).
#   * shared namespace: never bind-all (the VPN peer would be offered the port);
#     probe A2Y_METRICS_PROBE_HOST and bind the one address that faces it.
#   * neither set: metrics off. An agent must not fail to start because its
#     telemetry has nowhere to go.
if [ -n "${ACP2API_PORT:-}" ] && [ -z "${ACP2API_METRICS_ADDR:-}" ]; then
    _mport="${ACP2API_METRICS_PORT:-$((ACP2API_PORT + 8))}"
    if [ -n "${A2Y_METRICS_BIND:-}" ]; then
        export ACP2API_METRICS_ADDR="${A2Y_METRICS_BIND}:${_mport}"
        log "  metrics on ${ACP2API_METRICS_ADDR}"
    elif [ -n "${A2Y_METRICS_PROBE_HOST:-}" ]; then
        _bind="$(/opt/agent/hermes-agent/venv/bin/python3 -c 'import os,socket,sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
try:
    s.connect((os.environ["A2Y_METRICS_PROBE_HOST"], 9090))
    print(s.getsockname()[0])
except Exception:
    sys.exit(1)' 2>/dev/null || true)"
        if [ -n "$_bind" ]; then
            export ACP2API_METRICS_ADDR="${_bind}:${_mport}"
            log "  metrics on ${ACP2API_METRICS_ADDR}"
        else
            export ACP2API_METRICS_ADDR="off"
            log "  ${A2Y_METRICS_PROBE_HOST} not resolvable -- metrics off"
        fi
    else
        export ACP2API_METRICS_ADDR="off"
        log "  metrics off"
    fi
fi

if [ "${A2Y_SHARED_NAMESPACE:-}" = "1" ]; then
    log "Step 1: Shared-namespace repairs"
    # A container that shares another's namespace inherits the OWNER's
    # resolv.conf -- typically a public resolver that knows nothing about docker
    # networks. 127.0.0.11 is docker's embedded resolver: it lives IN the
    # namespace, answers for every network the owner is attached to, and
    # forwards the rest upstream (through the tunnel, if there is one).
    printf 'nameserver 127.0.0.11\noptions ndots:0\n' > /etc/resolv.conf \
        || log "WARNING: could not rewrite /etc/resolv.conf -- internal names may not resolve"

    if [ -n "${AGENT_VPN_IFACE:-}" ]; then
        # Fail closed: without this the agent would come up in the wrong
        # namespace and egress on the host's real address -- exactly what the
        # tunnel exists to prevent -- and nothing downstream would notice.
        # sysfs reflects the shared namespace and needs no tools.
        [ -d "/sys/class/net/${AGENT_VPN_IFACE}" ] \
            || die "interface ${AGENT_VPN_IFACE} is not in this network namespace -- refusing to start" "egress check"
        log "  tunnel interface ${AGENT_VPN_IFACE} present"
    fi
fi

log "Step 3: Installing the agent's identity from ${AGENT_CONFIG_DIR}"
# Copied on every start, not symlinked and not left to the volume: the rendered
# deploy tree is the source of truth for who this agent is. Hermes rewrites
# config.yaml when it migrates its schema, and those rewrites are deliberately
# discarded -- an identity that drifts inside a volume is one nobody can review.
mkdir -p "${HERMES_HOME}"
for f in config.yaml SOUL.md; do
    if [ -f "${AGENT_CONFIG_DIR}/${f}" ]; then
        install -m 0644 "${AGENT_CONFIG_DIR}/${f}" "${HERMES_HOME}/${f}"
        log "  ${f}"
    fi
done
# Plugins ship in the IMAGE: they are code, not identity, and the same for every
# agent. $HERMES_HOME/plugins is volume state, so they are copied in on every
# start -- the image decides what runs, not whatever the volume remembers.
if [ -d /opt/agent/plugins ]; then
    mkdir -p "${HERMES_HOME}/plugins"
    cp -a /opt/agent/plugins/. "${HERMES_HOME}/plugins/"
    log "  plugins: $(ls /opt/agent/plugins | tr '\n' ' ')"
fi

if [ -f "${AGENT_CONFIG_DIR}/hindsight.json" ]; then
    mkdir -p "${HERMES_HOME}/hindsight"
    install -m 0644 "${AGENT_CONFIG_DIR}/hindsight.json" "${HERMES_HOME}/hindsight/config.json"
    log "  hindsight/config.json"
fi

log "Step 3a: Applying the memory bank profiles Hindsight will not read for itself"
# The Hermes Hindsight plugin loads bank missions from the file just installed
# and then uses them NOWHERE -- no Banks API call exists in it. So the mission is
# live server state; this pushes the repository's version. It never clears a
# field, and it is never fatal: memory is deliberately not load-bearing.
: "${AGENT_BANKS_DIR:=/banks}"
if [ -f "${AGENT_CONFIG_DIR}/hindsight.json" ]; then
    /opt/agent/bin/apply-memory-profile.py \
        "${AGENT_CONFIG_DIR}/hindsight.json" "${AGENT_BANKS_DIR}" \
        || log "  WARNING: bank profiles not updated -- extraction keeps its previous steering"
fi

log "Step 3b: Appending the fleet roster to SOUL.md"
# A script rather than a heredoc because it runs TWICE: here, and on a loop from
# supervisord -- so an agent added to the fleet is found WITHOUT restarting the
# others. /fleet is a bind mount of the deploy tree's agents/, and Hermes
# re-reads SOUL.md once per session.
: "${AGENT_FLEET_DIR:=/fleet}"
/opt/agent/bin/fleet-roster.py \
    "${AGENT_FLEET_DIR}" "${AGENT_CONFIG_DIR}/SOUL.md" "${HERMES_HOME}/SOUL.md" "${AGENT_NAME}" \
    || log "  WARNING: roster generation failed -- SOUL.md keeps whatever it had"

log "Step 3c: Turning codex's own sandbox off for the human CLI"
# The ACP path is governed by `mode:` in acp2api.yaml (codex-acp sends a
# sandboxPolicy per TURN and never consults config.toml); this covers the codex
# CLI a human runs by hand in the container. Written with a guard rather than a
# blind append, because this file also holds the OAuth login state codex writes.
mkdir -p /root/.codex
if [ -f /root/.codex/config.toml ] && grep -q '^[[:space:]]*sandbox_mode[[:space:]]*=' /root/.codex/config.toml; then
    log "  sandbox_mode already set"
else
    printf 'sandbox_mode = "danger-full-access"\n' >> /root/.codex/config.toml
    log "  sandbox_mode = danger-full-access"
fi

log "Step 3d: Preparing git and ssh for the agent"
# Only agents with a /root/.ssh volume get any of this -- an agent with no
# business on a host simply has no such mount.
if [ -d /root/.ssh ]; then
    chmod 700 /root/.ssh

    # A SEPARATE key from the host-access one: revoking the deploy key must not
    # take out ssh to the hosts, and vice versa. Generated once and kept --
    # regenerating would mean re-registering the deploy key on every restart.
    if [ ! -f /root/.ssh/id_git_ed25519 ]; then
        ssh-keygen -q -t ed25519 -N "" -C "${AGENT_NAME}-git" -f /root/.ssh/id_git_ed25519
        log "  generated a git key -- register the PUBLIC half as a deploy key:"
        log "  $(cat /root/.ssh/id_git_ed25519.pub)"
    fi

    # Forge host keys are public and stable; seeding them here means a wiped
    # volume still verifies rather than hanging on a prompt. github.com is the
    # default; A2Y_GIT_HOSTS (comma-separated) adds a gitea/gitlab of yours.
    for _host in github.com ${A2Y_GIT_HOSTS//,/ }; do
        if ! grep -q "^${_host} " /root/.ssh/known_hosts 2>/dev/null; then
            ssh-keyscan -t rsa,ecdsa,ed25519 "${_host}" >> /root/.ssh/known_hosts 2>/dev/null \
                && log "  known_hosts: added ${_host}"
        fi
    done
    chmod 600 /root/.ssh/known_hosts 2>/dev/null || true

    # Which key for which host: without this ssh offers the host-access key
    # first and enough rejections abort the connection.
    cat > /root/.ssh/config <<SSHCFG
Host github.com
    User git
    IdentityFile /root/.ssh/id_git_ed25519
    IdentitiesOnly yes

Host *
    IdentityFile /root/.ssh/id_ed25519
    IdentitiesOnly yes
SSHCFG
    chmod 600 /root/.ssh/config
fi

# Commits carry the agent's name, so `git log` attributes its work.
if [ -d /root/.ssh ] || [ -n "${GH_TOKEN:-}" ]; then
    cat > /root/.gitconfig <<GITCFG
[user]
	name = ${AGENT_NAME}
	email = ${AGENT_NAME}@agents.local
[init]
	defaultBranch = main
[safe]
	directory = *
[pull]
	ff = only
GITCFG
    log "  git identity: ${AGENT_NAME}"
fi

# A token instead of a key, for agents that own product repositories. gh reads
# GH_TOKEN on its own; git has to be told -- including the ssh forms, because a
# dependency manifest can hardcode git+ssh://git@github.com/ and `yarn install`
# would then demand a key the agent deliberately does not have.
if [ -n "${GH_TOKEN:-}" ]; then
    for form in "https://github.com/" "ssh://git@github.com/" "git@github.com:"; do
        git config --global "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf" "$form"
    done
    log "  github: token from GH_TOKEN, and git rewrites ssh remotes onto it"
fi

log "Step 4: Writing ${HERMES_HOME}/.env from the container environment"
# Hermes reads credentials from this file, not from the process environment. Only
# the variables listed cross over -- a blanket dump would hand every unrelated
# secret in the container to the agent's own tooling.
#
# The presence of a platform token is what ENABLES that platform's gateway;
# there is no separate switch.
: > "${HERMES_HOME}/.env"
chmod 0600 "${HERMES_HOME}/.env"
_cross() {
    local var
    for var in "$@"; do
        if [ -n "${!var:-}" ]; then
            printf '%s=%s\n' "$var" "${!var}" >> "${HERMES_HOME}/.env"
        fi
    done
}
_cross \
    MATTERMOST_URL MATTERMOST_TOKEN MATTERMOST_ALLOWED_USERS \
    MATTERMOST_ALLOWED_CHANNELS MATTERMOST_HOME_CHANNEL \
    MATTERMOST_REPLY_MODE MATTERMOST_REQUIRE_MENTION \
    MATTERMOST_FREE_RESPONSE_CHANNELS \
    HINDSIGHT_API_URL HINDSIGHT_API_KEY HINDSIGHT_MODE HINDSIGHT_BANK_ID \
    OPENAI_API_KEY AGENT_PEERS AGENT_DESCRIPTION
# Other Hermes platform adapters, passed through by prefix so a fleet on
# telegram/slack/discord needs no entrypoint edit -- set the adapter's variables
# via platform.env in fleet.yaml and they arrive here.
for var in $(compgen -e | grep -E '^(TELEGRAM|SLACK|DISCORD)_' || true); do
    _cross "$var"
done
# And the fully generic route: A2Y_HERMES_ENV_EXTRA names container variables
# (comma-separated) to cross over -- for a platform or plugin this whitelist
# does not know, declared per agent as `hermes_env:` in its manifest.
if [ -n "${A2Y_HERMES_ENV_EXTRA:-}" ]; then
    for var in ${A2Y_HERMES_ENV_EXTRA//,/ }; do
        _cross "$var"
    done
fi
log "  $(wc -l < "${HERMES_HOME}/.env") variable(s)"

log "Step 5: Starting acp2api :${ACP2API_PORT}, litellm :${LITELLM_PORT}, hermes gateway"
# supervisord in the foreground so it receives SIGTERM directly and stops the
# children in order. It is not PID 1 -- tini is -- because the coding CLIs spawn
# ACP and MCP subprocesses supervisord never learns about and would never reap.
exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
