#!/usr/bin/env bash
#
# Start everything the web console needs, locally, with one command:
#
#   1. proxy.sh, if nothing is already serving the MCP endpoint on MCP_PORT.
#      The console needs an authenticated tunnel to the private Cloud Run
#      service; `gcloud run services proxy` supplies the credential.
#   2. the console, with whichever model key is in .env -- ANTHROPIC_API_KEY or
#      GEMINI_API_KEY. Without either the console still starts: the chat mode is
#      not offered and the tool mode -- pick a tool, fill in its parameters --
#      is, which needs no model and no account anywhere.
#
# Then it prints the URL with the access key already in it, and waits. Ctrl+C
# shuts down whatever this script started -- and only that: a proxy that was
# already running when we arrived is left alone, because something else (a
# Claude Code `goodreads-remote` registration, most likely) is using it.
#
# Nothing here is for production. deploy-chat.sh is the deployed path, where
# the identity token is minted per request from the metadata server and both
# secrets come from Secret Manager.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

ENV_FILE="${ENV_FILE:-.env}"
VENV="${VENV:-.venv}"

PROXY_PID=""            # set only if THIS script started the proxy
CHAT_PID=""
STARTED_PROXY=0
SIGNALLED=0

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[1merror:\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

# Prints something if the MCP endpoint is being served, nothing otherwise.
mcp_owner() {
  curl -sf --max-time 2 "http://127.0.0.1:${MCP_PORT}/health" 2>/dev/null \
    | grep -o '"service":"[^"]*"' || true
}

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
#
# proxy.sh execs gcloud, which spawns the cloud-run-proxy binary as a child, so
# killing the script's own pid leaves the tunnel running. It is started with
# setsid below to get its own process group, and the whole group is signalled
# here.
#
# The exit status is preserved: this runs from the EXIT trap too, where a
# failure must not be turned into a success by the cleanup itself.

cleanup() {
  local status=$?
  [ "${SIGNALLED}" -eq 1 ] && status=0
  trap - INT TERM EXIT          # a second Ctrl+C must not re-enter this
  printf '\n'
  bold "shutting down"

  if [ -n "${CHAT_PID}" ] && kill -0 "${CHAT_PID}" 2>/dev/null; then
    info "stopping the console (pid ${CHAT_PID})"
    kill -TERM "${CHAT_PID}" 2>/dev/null
    for _ in $(seq 1 20); do
      kill -0 "${CHAT_PID}" 2>/dev/null || break
      sleep 0.25
    done
    kill -KILL "${CHAT_PID}" 2>/dev/null
  fi

  if [ "${STARTED_PROXY}" -eq 1 ] && [ -n "${PROXY_PID}" ]; then
    info "stopping the MCP proxy (process group ${PROXY_PID})"
    kill -TERM -- "-${PROXY_PID}" 2>/dev/null || kill -TERM "${PROXY_PID}" 2>/dev/null
    for _ in $(seq 1 20); do
      kill -0 "${PROXY_PID}" 2>/dev/null || break
      sleep 0.25
    done
    kill -KILL -- "-${PROXY_PID}" 2>/dev/null
  elif [ "${STARTED_PROXY}" -eq 0 ] && [ -n "$(mcp_owner)" ]; then
    info "leaving the MCP proxy up — it was already running before this script"
  fi

  info "done"
  exit "${status}"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

bold "goodreads-stats console — local"

[ -x "${VENV}/bin/goodreads-chat" ] || die \
"the console is not installed in ${VENV}.

  ${VENV}/bin/pip install -e '.[web]'"

command -v curl >/dev/null || die "curl is required for the readiness checks"

# --- .env ------------------------------------------------------------------
#
# Parsed rather than sourced: this file holds secrets, and `.` would execute
# whatever is in it. Only KEY=value lines are read, split on the first =.
# Parsed before the defaults below, so .env can set PORT and friends too.

if [ ! -f "${ENV_FILE}" ]; then
  ( umask 177; cat > "${ENV_FILE}" <<'ENVEOF'
# Local secrets for the web console. Gitignored; never commit this file.
#
# Both model keys are optional, and either one turns the chat mode on; without
# either, only the tool mode -- which needs no model at all. Set both and
# CHAT_PROVIDER decides, defaulting to anthropic.
#
# Get an Anthropic key from https://console.anthropic.com/
ANTHROPIC_API_KEY=

# Or a Google AI Studio key from https://aistudio.google.com/apikey -- its free
# tier runs the chat mode with no paid credits, which is the cheapest way to
# see the console with a model behind it.
GEMINI_API_KEY=

# CHAT_ACCESS_TOKEN gates the console. run-local.sh generates one here on first
# run if it is empty, so the ?k= URL stays the same between runs.
CHAT_ACCESS_TOKEN=
ENVEOF
  )
  info "no ${ENV_FILE}, so I created one (gitignored, chmod 600)."
  info "put an ANTHROPIC_API_KEY or GEMINI_API_KEY in it for the chat mode too."
fi

while IFS= read -r line || [ -n "${line}" ]; do
  line="${line#"${line%%[![:space:]]*}"}"       # trim leading whitespace
  case "${line}" in
    ''|'#'*) continue ;;
    *=*) ;;
    *) continue ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  case "${key}" in
    ''|*[!A-Za-z0-9_]*) continue ;;             # not a shell-safe name
  esac
  value="${value%\"}"; value="${value#\"}"      # strip one layer of quoting
  value="${value%\'}"; value="${value#\'}"
  # Already-exported values win, so `ANTHROPIC_API_KEY=... ./run-local.sh`
  # overrides the file without editing it.
  if [ -z "${!key:-}" ]; then
    export "${key}=${value}"
  fi
done < "${ENV_FILE}"

# --- defaults, after .env so the file can set them -------------------------

PORT="${PORT:-8081}"                  # the console
MCP_PORT="${MCP_PORT:-8080}"          # proxy.sh's local end
LOG_DIR="${LOG_DIR:-logs}"
PROXY_TIMEOUT="${PROXY_TIMEOUT:-60}"
CHAT_TIMEOUT="${CHAT_TIMEOUT:-40}"

mkdir -p "${LOG_DIR}"
PROXY_LOG="${LOG_DIR}/proxy.log"
CHAT_LOG="${LOG_DIR}/chat.log"

# --- which modes this run will offer ---------------------------------------
#
# Not a precondition. The console decides from the environment it is given, and
# the browser is told which modes exist by /api/health; this is only so the
# person reading the terminal knows before they click.

# Asked of the console's own configuration rather than recomputed here. Which
# key wins, and what CHAT_PROVIDER does when both are set, is decided in
# config.py; a second copy of that rule in bash would drift from the masthead
# this line is meant to match. Prints "<status><tab><detail>".
PROVIDER_LINE="$("${VENV}/bin/python" - 2>/dev/null <<'PYEOF' || true
from webchat import config, provider

try:
    name = provider.chosen()
except provider.ProviderError as exc:
    print(f"error\t{exc}")
else:
    print(f"ok\t{name} · {config.active_model()}" if name else "none\t")
PYEOF
)"
PROVIDER_STATUS="${PROVIDER_LINE%%$'\t'*}"
PROVIDER_DETAIL="${PROVIDER_LINE#*$'\t'}"

case "${PROVIDER_STATUS}" in
  ok)
    # The same shape the masthead shows: provider · model.
    MODES="chat and tool modes — ${PROVIDER_DETAIL}"
    ;;
  error)
    die "${PROVIDER_DETAIL}

CHAT_PROVIDER names a provider whose key is not set. Fix it in ${ENV_FILE}, or
unset it and let the key that is present decide."
    ;;
  none)
    MODES="tool mode only — no ANTHROPIC_API_KEY or GEMINI_API_KEY"
    ;;
  *)
    # The console's own config could not be read. Say so rather than claiming a
    # mode: guessing is how the terminal ends up disagreeing with the page.
    MODES="modes unknown — could not read the console configuration"
    ;;
esac

# --- access token ----------------------------------------------------------

if [ -z "${CHAT_ACCESS_TOKEN:-}" ]; then
  if command -v openssl >/dev/null; then
    CHAT_ACCESS_TOKEN="$(openssl rand -hex 24)"
  else
    CHAT_ACCESS_TOKEN="$("${VENV}/bin/python" -c 'import secrets; print(secrets.token_hex(24))')"
  fi
  export CHAT_ACCESS_TOKEN

  # Persisted so the ?k= URL survives a restart -- a fresh token every run
  # would invalidate the browser's cookie, and the bookmark with it.
  if grep -q '^CHAT_ACCESS_TOKEN=[[:space:]]*$' "${ENV_FILE}" 2>/dev/null; then
    tmp="$(mktemp)"
    sed "s|^CHAT_ACCESS_TOKEN=[[:space:]]*$|CHAT_ACCESS_TOKEN=${CHAT_ACCESS_TOKEN}|" \
      "${ENV_FILE}" > "${tmp}"
    cat "${tmp}" > "${ENV_FILE}"      # write through, preserving the file's mode
    rm -f "${tmp}"
  else
    printf 'CHAT_ACCESS_TOKEN=%s\n' "${CHAT_ACCESS_TOKEN}" >> "${ENV_FILE}"
  fi
  chmod 600 "${ENV_FILE}" 2>/dev/null || true
  GENERATED_TOKEN=1
else
  GENERATED_TOKEN=0
fi

trap 'SIGNALLED=1; cleanup' INT TERM
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. The MCP proxy
# ---------------------------------------------------------------------------

if [ -n "$(mcp_owner)" ]; then
  info "MCP endpoint already served on 127.0.0.1:${MCP_PORT} — reusing it"
else
  if command -v setsid >/dev/null; then
    setsid ./proxy.sh > "${PROXY_LOG}" 2>&1 &
  else
    ./proxy.sh > "${PROXY_LOG}" 2>&1 &
  fi
  PROXY_PID=$!
  STARTED_PROXY=1
  info "starting proxy.sh (pid ${PROXY_PID}, log ${PROXY_LOG})"

  ready=0
  for _ in $(seq 1 "${PROXY_TIMEOUT}"); do
    if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
      printf '\n' >&2
      sed 's/^/    /' "${PROXY_LOG}" >&2
      die "proxy.sh exited. Its output is above and in ${PROXY_LOG}.

The usual cause is the standalone Cloud SDK missing the cloud-run-proxy
component; proxy.sh explains that at the top."
    fi
    if [ -n "$(mcp_owner)" ]; then ready=1; break; fi
    sleep 1
  done
  [ "${ready}" -eq 1 ] || die "the proxy did not answer /health within ${PROXY_TIMEOUT}s; see ${PROXY_LOG}"
  info "MCP endpoint ready on 127.0.0.1:${MCP_PORT}"
fi

# ---------------------------------------------------------------------------
# 2. The console
# ---------------------------------------------------------------------------

if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  die "something is already listening on 127.0.0.1:${PORT}.

Stop it, or pick another port:  PORT=8082 ./run-local.sh"
fi

export GOODREADS_MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
# The proxy injects the Cloud Run credential, so the console must not send one
# of its own. Setting either of these would be wrong on this path, and the
# console treats an audience plus a static token as a startup error.
unset GOODREADS_MCP_AUDIENCE GOODREADS_MCP_TOKEN
# Plain HTTP on localhost, so a Secure cookie would never come back.
export CHAT_COOKIE_SECURE="${CHAT_COOKIE_SECURE:-0}"
export PORT

"${VENV}/bin/goodreads-chat" > "${CHAT_LOG}" 2>&1 &
CHAT_PID=$!
info "starting the console (pid ${CHAT_PID}, log ${CHAT_LOG})"

ready=0
for _ in $(seq 1 "${CHAT_TIMEOUT}"); do
  if ! kill -0 "${CHAT_PID}" 2>/dev/null; then
    printf '\n' >&2
    tail -20 "${CHAT_LOG}" | sed 's/^/    /' >&2
    die "the console exited during startup. Its log tail is above."
  fi
  if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "${ready}" -eq 1 ] || die "the console did not answer /api/health within ${CHAT_TIMEOUT}s; see ${CHAT_LOG}"

# ---------------------------------------------------------------------------
# Ready
# ---------------------------------------------------------------------------

TOOLS="$(curl -sf --max-time 5 -H "x-chat-access: ${CHAT_ACCESS_TOKEN}" \
  "http://127.0.0.1:${PORT}/api/health" 2>/dev/null | grep -o '"tools":[0-9]*' | cut -d: -f2)"

printf '\n'
bold "ready — open this:"
printf '\n    \033[4mhttp://127.0.0.1:%s/?k=%s\033[0m\n\n' "${PORT}" "${CHAT_ACCESS_TOKEN}"
info "the ?k= key is needed once; after that it lives in an HttpOnly cookie"
[ -n "${TOOLS}" ] && info "${TOOLS} tools discovered over MCP"
info "${MODES}"
if [ "${PROVIDER_STATUS}" = "none" ]; then
  info "  pick a tool and fill in its parameters; the form is built from its schema"
  info "  for the chat mode too, add either key to ${ENV_FILE}:"
  info "    ANTHROPIC_API_KEY  https://console.anthropic.com/"
  info "    GEMINI_API_KEY     https://aistudio.google.com/apikey — free tier, no credits"
fi
[ "${GENERATED_TOKEN}" -eq 1 ] && info "generated a CHAT_ACCESS_TOKEN and saved it to ${ENV_FILE}"
info "logs: ${CHAT_LOG}  ${PROXY_LOG}"
printf '\n'
bold "Ctrl+C to stop."

# Waiting on the console keeps this script in the foreground so the traps fire.
# `wait` returns as soon as a signal arrives, so re-wait until it really exits.
while kill -0 "${CHAT_PID}" 2>/dev/null; do
  wait "${CHAT_PID}" 2>/dev/null && break
done

printf '\n' >&2
tail -20 "${CHAT_LOG}" | sed 's/^/    /' >&2
die "the console exited on its own. Its log tail is above."
