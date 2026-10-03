# Sourced by run.sh and cards-web.sh after ~/.zshrc.local, so nothing set there can point the connector elsewhere.
# On a Mac with the clean copy of the harness's main branch (what its owner merged), that copy is the private folder:
# never the working copy, whose files may hold changes nobody approved yet. Without a clean copy, private.py's default
# stands.
_clean="$HOME/Library/Application Support/personal-agent-harness"
if [[ -d $_clean ]]; then
  export LIFE_MCP_PRIVATE=$_clean
fi
unset _clean
