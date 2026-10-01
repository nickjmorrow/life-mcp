#!/bin/zsh
# Launched hourly by launchd (com.nicholai.health-import). Secrets come from ~/.zshrc.local.
source ~/.zshrc.local
cd "${0:A:h}"
exec /opt/homebrew/bin/uv run --quiet --frozen health_import.py  # deps: pyproject.toml + uv.lock
