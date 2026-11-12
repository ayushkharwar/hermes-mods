#!/usr/bin/env bash
# Copy the plugins and widgets into a Hermes install and enable the plugins.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v hermes >/dev/null 2>&1; then
  echo "hermes is not on PATH. Install Hermes Agent first." >&2
  exit 1
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS="$HERMES_HOME/plugins"
WIDGETS="$HERMES_HOME/tui-widgets"

mkdir -p "$PLUGINS" "$WIDGETS"

for mod in savvy-progress filetree-rail keepwarm; do
  echo "plugin  -> $PLUGINS/$mod"
  mkdir -p "$PLUGINS/$mod"
  cp -f "plugins/$mod/plugin.yaml" "plugins/$mod/__init__.py" "$PLUGINS/$mod/"
done

for widget in savvy-progress filetree; do
  echo "widget  -> $WIDGETS/$widget.mjs"
  cp -f "tui-widgets/$widget.mjs" "$WIDGETS/"
done

for mod in savvy-progress filetree-rail keepwarm; do
  echo "enable  -> $mod"
  hermes plugins enable "$mod" || echo "  (already enabled or enabling failed)"
done

echo
echo "Done. In hermes --tui try: /progress, /explorer, /keepwarm"
