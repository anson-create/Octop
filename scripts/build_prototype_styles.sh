#!/usr/bin/env sh
# Build the stylesheet bundle a derived prototype inlines.
#
# Why not just `vite build`: a full build of the app exhausts the heap on a
# 6 GB box (SIGABRT), and it bundles every route when all we need is CSS. This
# compiles exactly three things in the app's own order — the UI library, Tailwind
# with the app's config, and the SCSS entry — and concatenates them.
#
# The result matters for fidelity: inlining raw `.scss` would put
# `$color-primary: #165DFF` into a <style> tag, where the browser drops it as
# invalid and the prototype silently loses the product's palette.
#
# Usage: build_prototype_styles.sh <mirror-root>
# Run once after each source sync; re-running is cheap and idempotent.

set -eu

ROOT="${1:-}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
    echo "usage: $0 <mirror-root>" >&2
    exit 2
fi

cd "$ROOT"

UI_CSS="node_modules/@arco-design/web-vue/dist/arco.css"
if [ ! -f "$UI_CSS" ]; then
    echo "missing $UI_CSS — run npm install in $ROOT first" >&2
    exit 3
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

npx tailwindcss -i src/styles/tailwind.css -o "$TMP/tailwind.css" --minify >/dev/null

if [ -f src/styles/index.scss ]; then
    npx sass --no-source-map --style=compressed src/styles/index.scss "$TMP/index.css"
else
    : >"$TMP/index.css"
fi

if [ -f src/styles/platform-admin.scss ]; then
    npx sass --no-source-map --style=compressed src/styles/platform-admin.scss "$TMP/platform-admin.css"
else
    : >"$TMP/platform-admin.css"
fi

mkdir -p dist
cat "$UI_CSS" "$TMP/tailwind.css" "$TMP/index.css" "$TMP/platform-admin.css" >dist/pm-prototype.css

echo "wrote dist/pm-prototype.css ($(wc -c <dist/pm-prototype.css) bytes)"
