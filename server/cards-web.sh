#!/bin/zsh
# Launched by launchd (com.nicholai.cards-web): the flashcard review page on 127.0.0.1:8769.
source ~/.zshrc.local
cd "${0:A:h}"
exec /opt/homebrew/bin/uv run --quiet --frozen cards_web.py
