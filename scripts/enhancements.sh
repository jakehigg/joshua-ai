#!/bin/sh
# Print `-f enhancements/<slot>/<choice>/docker-compose.yml` for each slot in
# enhancements.yaml whose `use` is not `none`. The Makefile adds this output
# to the compose command line, so an enabled choice joins the stack the same
# way the base file does.
#
# Usage: scripts/enhancements.sh [yaml_file]
#   yaml_file   defaults to enhancements.yaml in the repo root. A test passes
#               another path.
#
# The file has one two-level shape:
#
#   enhancements:
#     <slot>:
#       use: <choice>
#
# A slot is a role, such as `wiki`. A choice names the container that fills
# it, or `custom` for one you supply yourself at
# enhancements/<slot>/custom/docker-compose.yml (copy the
# docker-compose.example.yml next to it first). `use: none`, or a slot with
# no `use:` line, turns a slot off and prints nothing for it.
#
# A slot name and a choice must be lowercase letters, digits, and `-`; any
# other value is an error. A `#` comment and a blank line are ignored. A
# missing yaml_file prints nothing and exits 0. No Python runs here: the
# host does not need it.
set -eu

cd "$(dirname "$0")/.."

yaml_file="${1:-enhancements.yaml}"

[ -f "$yaml_file" ] || exit 0

# Pull "<slot><TAB><choice>" pairs out of the two-level block under
# `enhancements:`. Only the first indented level under it names a slot; only
# a `use:` line one level deeper than that names its choice. Any other key,
# or a deeper mapping, is ignored, so the format has room to grow.
pairs=$(awk '
    { sub(/#.*/, ""); sub(/[ \t\r]+$/, "") }
    /^[ \t]*$/ { next }
    /^enhancements:[ \t]*$/ { in_block = 1; slot_indent = -1; next }
    {
        match($0, /^[ \t]*/)
        indent = RLENGTH
        if (!in_block || indent == 0) { in_block = 0; next }
        if (slot_indent < 0) { slot_indent = indent }
        line = $0
        sub(/^[ \t]*/, "", line)
        if (indent == slot_indent) {
            if (match(line, /^[^:]+:[ \t]*$/)) {
                slot = line
                sub(/:[ \t]*$/, "", slot)
            } else {
                slot = ""
            }
            next
        }
        if (indent > slot_indent && slot != "" && match(line, /^use:[ \t]*/)) {
            choice = substr(line, RLENGTH + 1)
            printf "%s\t%s\n", slot, choice
        }
    }
' "$yaml_file")

[ -n "$pairs" ] || exit 0

printf '%s\n' "$pairs" | while IFS="$(printf '\t')" read -r slot choice; do
    [ -n "$choice" ] || continue
    [ "$choice" = "none" ] && continue

    case "$slot" in
        *[!a-z0-9-]* | "")
            echo "enhancements.sh: $yaml_file has an invalid slot name '$slot'" >&2
            exit 1
            ;;
    esac
    case "$choice" in
        *[!a-z0-9-]*)
            echo "enhancements.sh: $yaml_file sets $slot.use to an invalid choice '$choice'" >&2
            exit 1
            ;;
    esac

    dir="enhancements/$slot/$choice"
    file="$dir/docker-compose.yml"
    if [ ! -f "$file" ]; then
        if [ "$choice" = "custom" ]; then
            echo "enhancements.sh: $yaml_file sets $slot.use: custom, but $file does not exist. Copy $dir/docker-compose.example.yml to $dir/docker-compose.yml and edit it." >&2
        else
            echo "enhancements.sh: $yaml_file sets $slot.use: $choice, but $file does not exist" >&2
        fi
        exit 1
    fi
    echo "-f $file"
done
