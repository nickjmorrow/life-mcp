# Sourced by run.sh and cards-web.sh after ~/.zshrc.local, so nothing set there can point the connector elsewhere.
# On a Mac with the harness's live tree (the last commit its owner approved), that tree is the private folder: never
# the checkout, whose files may hold changes nobody approved yet. Without a live tree, private.py's default stands.
_live="$HOME/Library/Application Support/personal-agent/live"
if [[ -d $_live ]]; then
  export LIFE_MCP_PRIVATE=$_live
fi
unset _live
