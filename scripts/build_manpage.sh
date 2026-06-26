#!/usr/bin/env bash
# Build a gzipped man page from a markdown source.
#
# Usage: build_manpage.sh <name> <version> <date> <input.md> <output.1.gz>
#
# Composes a pandoc-style YAML header with m4, prepends it to the markdown,
# pipes through pandoc, then gzip -n for reproducibility. Honors $M4 and
# $PANDOC env vars so Bazel can supply hermetic toolchains; falls back to PATH.
set -euo pipefail

NAME=$1
VERSION=$2
DATE=$3
INPUT=$4
OUTPUT=$5

: "${M4:=m4}"
: "${PANDOC:=pandoc}"

HEADER=$("$M4" \
    -DTITLE="$NAME" \
    -DVERSION="$VERSION" \
    -DDATE="$DATE" \
    <<'EOF'
---
title: TITLE
section: 1
header: Revup Manual
footer: revup VERSION
date: DATE
---
EOF
)

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

printf '%s\n' "$HEADER" | cat - "$INPUT" \
    | "$PANDOC" -f markdown-smart -s -t man > "$TMP/page.1"
gzip -n -c "$TMP/page.1" > "$OUTPUT"
