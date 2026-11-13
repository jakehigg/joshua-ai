#!/bin/sh
# Print the compose profiles that joshua.yaml turns on, as a comma list. The
# Makefile exports the output as COMPOSE_PROFILES, so `make up`, `make up-dev`,
# and `make down` start and stop the optional containers with the stack.
#
#   viewer        when `viewer.enabled: true`
#   assignments   when `assignments.enabled: true`
#
# Usage: scripts/profiles.sh [yaml_file]
#   yaml_file   defaults to joshua.yaml in the repo root. A test passes
#               another path.
#
# Only `enabled:` on the first indented level under the top-level key counts.
# A `#` comment and a blank line are ignored. A missing yaml_file prints
# nothing. No Python runs here: the host does not need it.
set -eu

cd "$(dirname "$0")/.."

yaml_file="${1:-joshua.yaml}"

[ -f "$yaml_file" ] || exit 0

awk '
    { sub(/#.*/, ""); sub(/[ \t\r]+$/, "") }
    /^[ \t]*$/ { next }
    {
        match($0, /^[ \t]*/)
        indent = RLENGTH
        if (indent == 0) {
            section = $0
            sub(/:.*/, "", section)
            child = -1
            next
        }
        if (section != "viewer" && section != "assignments") { next }
        if (child < 0) { child = indent }
        if (indent != child) { next }
        line = substr($0, indent + 1)
        if (line ~ /^enabled:[ \t]*true$/) { on[section] = 1 }
    }
    END {
        out = ""
        if (on["viewer"]) { out = "viewer" }
        if (on["assignments"]) { out = out (out == "" ? "" : ",") "assignments" }
        print out
    }
' "$yaml_file"
