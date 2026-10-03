#!/bin/zsh
# Launched by launchd (com.nicholai.life-mcp). Loads secrets from
# ~/.zshrc.local at runtime so they never live in the plist or this repo.
source ~/.zshrc.local
cd "${0:A:h}"
source ./private-env.sh  # after ~/.zshrc.local: the private folder is the clean copy whenever there is one
exec /opt/homebrew/bin/uv run --quiet --frozen server.py  # deps: pyproject.toml + uv.lock
