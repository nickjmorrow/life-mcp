# life-mcp

The Life connector: one remote MCP server that connects an agent (claude.ai, Claude Code, anything that speaks MCP) to the software in Nicholas's life — Logseq, Apple Health and Hevy, Eight Sleep, Contacts and Messages, Reminders, Music and the HomePods, the Apple TV, Apple Home and Hue. This file covers the code; how his agent uses it lives in his private agent harness.

**This repo is public.** Nothing personal goes in it: no other people's names or details, health facts, addresses, hostnames, IP addresses, account or device IDs, plans or history. Personal settings come from the private folder (below), secrets from the environment. Tests use made-up people and places and documentation IPs (192.0.2.x).

## How it fits together

```
claude.ai (phone, web) ─┐                                  ┌─ Logseq graph (notes, journal, Claude memories)
Claude Code ─────────────┼─► Tailscale Funnel ─► server/ ───┼─ health.db (Apple Health, Hevy, Eight Sleep)
other agents ───────────┘   (HTTPS, GitHub      (FastMCP,   ├─ Messages + Contacts copies (people)
                             sign-in, one        one Mac at  ├─ Hue, Eight Sleep, Reminders, Music/HomePods,
                             account)            home)       │  Apple TV, Apple Home (via home-app/)
                                                             └─ skills (from the private folder)
```

## Parts

- **`server/`** — the connector. `server.py` builds one FastMCP server with GitHub OAuth (only the account in `LIFE_MCP_GITHUB_ID`, re-checked on every request), serves the Logseq tools itself, and mounts every tool group listed in its `GROUPS` table with `mount_all()` (a group that fails to import is logged and skipped): `memory_mcp.py` (shared memory on a Logseq page), `skills_mcp.py` (skills and their index), `health_mcp.py` + `health_import.py` (an hourly importer into SQLite), `people_mcp.py` + `people_data.py` (read-only copies of Messages and Contacts made by `people/people-snapshot.swift`, the only thing with Full Disk Access), `cards_mcp.py` (flashcard review), `reminders_mcp.py`, `music_mcp.py` / `music_lib_mcp.py` (Music.app and HomePods), `tv_mcp.py` (Apple TV via pyatv; screenshots via Xcode's devicectl), `home_mcp.py`, `eightsleep_proxy.py` (the npm Eight Sleep server) + `eightsleep_mcp.py`, `hevy_mcp.py` (Hevy's API with the key kept server-side). A new group is one line in `GROUPS` plus a check in `smoke.py` (a test keeps them in step). It listens on 127.0.0.1 only; Tailscale Funnel is the public door. `run.sh` starts it under launchd (`com.nicholai.life-mcp`). Messages name the Mac it runs on (`host.NAME`), never a fixed machine.
- **`server/cards_web.py`** — a phone page over the same flashcard deck as `cards_mcp.py` (scheduling in `fsrs.py`, FSRS v4 on Logseq's own properties): it reads each card aloud with the browser's speech synthesis and takes Again / Hard / Good / Easy. It listens on 127.0.0.1 (`CARDS_PORT`, default 8769; launchd via `cards-web.sh`), is meant for `tailscale serve` (never Funnel), refuses any Host that isn't local or in the private config's `cards_hosts`, and needs an `X-Cards` header to write, so other web pages can't drive it.
- **`server/api.py`** — the supported interface for other code on the same Mac (his private lessons job): `cli(*args, json_out=False)`, `edn`, `ensure_properties`, `target_args`, `memory()` (a shared-memory handle with the memory tools' rules) and `people_note`. **Import `api`, nothing else**: the rest of `server/` can change without notice. Run such code with `PYTHONPATH=<this repo>/server uv run --project <this repo>/server python …`.
- **`hue/`** — Philips Hue tools (CLIP v2), mounted by the server as `hue_*`.
- **`home-app/`** — Life Home, a signed Mac Catalyst helper with the HomeKit entitlement (HomeKit has no API for command-line tools). It serves a loopback API on 127.0.0.1:8767 that refuses web pages (Host must be local, no Origin) and any request without the shared secret: the connector makes `~/.config/life-mcp/home-token` (600) and sends it as `X-Life-Home-Token`; the sandboxed app reads that one file through a read-only sandbox exception (see `home-app/FINDINGS.md`).
- **Settings and skills** come from a private folder outside this repo (`server/private.py`; `LIFE_MCP_PRIVATE`): its `config.toml` (Logseq graph name, Apple TV address, time zone, health database path) and its `skills/`. Personal routing (which app answers which kind of request) belongs in his skills there, not in the connector's instructions, which stay generic and factual. Without it the code runs with neutral defaults. Secrets come from the environment.

## Safety rules for the tools

- Text other people wrote (messages, person-page notes) is data, never instructions: the people tools fence it in a tag with a random id made per call (`people_data.untrusted`), so the text can't close the fence, and nothing from it goes into memory as a rule.
- `memory_save` / `memory_update` and `people_note` refuse text that names a connector tool (every tool the server exposes, registered by `mount_all`), tries to override instructions, or carries or asks for a secret (`memory_mcp.check_safe`). `memory_recall` starts with a line saying its entries are data, not instructions.
- The general Logseq tools refuse the Claude memories page (update, delete or move a block on it, or add, move or delete onto it), so the memory guard can't be walked around.
- Secrets never go into tool output or skill text.

## Working on it

- Dependencies: pinned in `server/pyproject.toml` (exact versions) and `server/uv.lock`, for everything in `server/` and `hue/`. Run scripts from `server/` with `uv run <script>.py` (launchd uses `uv run --frozen`); from `hue/` add `--project ../server`. To change one, edit `pyproject.toml` and run `uv lock`.
- Tests: `cd server && uv run pytest -q`; `cd hue && uv run --project ../server pytest -q`.
- Deploy: commit and push, then on the server Mac `git pull` and `launchctl kickstart -k gui/$(id -u)/com.nicholai.life-mcp`. Over SSH the server Mac's Logseq, Reminders, Music and code signing aren't reachable; run checks there as a one-shot launchd job.
- Keep this file current when the code changes. It's public: nothing personal goes in it.
