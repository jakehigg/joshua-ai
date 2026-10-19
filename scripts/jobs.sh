#!/usr/bin/env bash
# Run a core job from the laptop, and read its runs. See docs/jobs.md.
#
#   scripts/jobs.sh run <job> [--dry-run] [--arg key=value ...]
#   scripts/jobs.sh status <job>
#   scripts/jobs.sh runs <id>
#   scripts/jobs.sh list <job> [--limit N]
#
# `run` starts a run in the background and prints its run id. Without
# --dry-run the run writes the wiki: a journal entry and one commit. Each
# --arg is one job argument, and its value is a string. `runs <id>` prints the
# report of one run. `list <job>` prints the newest runs of that job.
#
# CORE_URL is the core address (default http://127.0.0.1:8081).
# JOSHUA_TOKEN_LAPTOP is the laptop token from .env:
#   export $(grep JOSHUA_TOKEN_LAPTOP .env)
#
# The answer is printed as indented JSON. The script exits 1 when core answers
# with a status that is not 2xx, or cannot be reached, and 2 on a usage error.
set -euo pipefail

core_url="${CORE_URL:-http://127.0.0.1:8081}"
core_url="${core_url%/}"

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

fail_usage() {
  echo "jobs: $1" >&2
  usage
}

check_job() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || fail_usage "not a job name: $1"
}

# request METHOD PATH [BODY_FILE]
# The token goes to curl on stdin, as a config file, and never in an argument:
# every user on the machine can read the argument list of every process.
request() {
  local method=$1 path=$2 body=${3:-} out status
  if [ -z "${JOSHUA_TOKEN_LAPTOP:-}" ]; then
    echo "jobs: set JOSHUA_TOKEN_LAPTOP (export \$(grep JOSHUA_TOKEN_LAPTOP .env))" >&2
    exit 2
  fi
  local -a extra=()
  if [ -n "$body" ]; then
    extra=(-H "Content-Type: application/json" --data-binary "@$body")
  fi
  out="$(mktemp)"
  if ! status="$(printf 'header = "Authorization: Bearer %s"\n' "$JOSHUA_TOKEN_LAPTOP" |
    curl -sS --max-time 60 --config - -X "$method" ${extra[@]+"${extra[@]}"} \
      -o "$out" -w '%{http_code}' "$core_url$path")"; then
    rm -f "$out"
    echo "jobs: cannot reach core at $core_url" >&2
    exit 1
  fi
  if ! python3 -m json.tool "$out" 2>/dev/null; then
    cat "$out"
    echo
  fi
  rm -f "$out"
  case "$status" in
    2??) return 0 ;;
    *)
      echo "jobs: core answered HTTP $status" >&2
      exit 1
      ;;
  esac
}

cmd_run() {
  [ $# -ge 1 ] || fail_usage "run needs a job name"
  local job=$1 dry=false
  shift
  check_job "$job"
  local -a pairs=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) dry=true ;;
      --arg)
        [ $# -ge 2 ] || fail_usage "--arg needs key=value"
        [[ "$2" == *=* && "${2%%=*}" != "" ]] || fail_usage "--arg needs key=value, not: $2"
        pairs+=("$2")
        shift
        ;;
      *) fail_usage "unknown option: $1" ;;
    esac
    shift
  done
  local body
  body="$(mktemp)"
  python3 - "$dry" ${pairs[@]+"${pairs[@]}"} >"$body" <<'PY'
import json
import sys

dry = sys.argv[1] == "true"
args = {}
for pair in sys.argv[2:]:
    key, value = pair.split("=", 1)
    args[key] = value
json.dump({"args": args, "dry_run": dry}, sys.stdout)
PY
  local rc=0
  (request POST "/admin/jobs/$job/run" "$body") || rc=$?
  rm -f "$body"
  return "$rc"
}

[ $# -ge 1 ] || usage
command=$1
shift
case "$command" in
  run) cmd_run "$@" ;;
  status)
    [ $# -eq 1 ] || fail_usage "status needs one job name"
    check_job "$1"
    request GET "/admin/jobs/$1/status"
    ;;
  runs)
    [ $# -eq 1 ] || fail_usage "runs needs one run id"
    [[ "$1" =~ ^[0-9]+$ ]] || fail_usage "not a run id: $1"
    request GET "/admin/jobs/runs/$1"
    ;;
  list)
    [ $# -ge 1 ] || fail_usage "list needs a job name"
    job=$1
    shift
    check_job "$job"
    limit=20
    if [ $# -gt 0 ]; then
      [ "$1" = "--limit" ] && [ $# -eq 2 ] || fail_usage "list takes only --limit N"
      [[ "$2" =~ ^[0-9]+$ ]] || fail_usage "not a number: $2"
      limit=$2
    fi
    request GET "/admin/jobs/runs?job=$job&limit=$limit"
    ;;
  -h | --help | help) usage ;;
  *) fail_usage "unknown command: $command" ;;
esac
