# life-mcp

One remote [MCP](https://modelcontextprotocol.io) server that lets every Claude I use (the phone app, the web, Claude Code) see and act on my life: my notes, my home, my health data, the people I talk to.

It runs on an old MacBook kept at home as a server. claude.ai reaches it through [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) over HTTPS, with GitHub sign-in locked to one account.

```
claude.ai (phone, web) ─┐                               ┌─ Logseq (notes, journal), shared memory (files in git)
Claude Code ─────────────┼─► Tailscale Funnel ─► server/ ┼─ health.db (Apple Health + Hevy + Eight Sleep)
other agents ───────────┘   GitHub OAuth, one account   ├─ Messages + Contacts (read-only copies)
                                                        ├─ Hue · Eight Sleep · Reminders · Music + HomePods
                                                        │  Apple TV · Apple Home · Hevy
                                                        ├─ skills (served from a private folder)
                                                        └─ changes: rule edits and ship it (merged by him elsewhere)
```

## What's here

| | |
| --- | --- |
| [`server/`](server) | The connector: a [FastMCP](https://gofastmcp.com) server with ~180 tools in groups (`memory_*`, `rule_edit`, `ship_it`, `health_*`, `people_*`, `cards_*`, `hue_*`, `home_*`, `tv_*`, `music_*`, `eight_sleep_*`, `reminders_*`, `hevy_api`, `skill_*`, plus the Logseq tools). Each group is its own module, listed in one table and mounted so one failing integration doesn't take down the rest. Dependencies are pinned in `server/pyproject.toml` + `uv.lock`. Hourly importers build a SQLite health database and private copies of Messages/Contacts. |
| [`hue/`](hue) | Philips Hue (CLIP v2): lights, effects, gradients, scenes, automations, rooms and zones. |
| [`home-app/`](home-app) | Life Home, a tiny signed Mac Catalyst app. HomeKit only talks to entitled apps, so this one serves Apple Home on a loopback-only API the server calls. |
| [`SYSTEM.md`](SYSTEM.md) | How the code fits together, for agents (and people) working on it. |

## Design notes

- **One public door.** Everything listens on 127.0.0.1; Funnel is the only way in, and every MCP request re-checks the GitHub account.
- **Private stays private.** Personal settings and skills live in a private folder the code reads at runtime; secrets live in environment variables. Nothing personal is in this repo or its history.
- **Prompt-injection aware.** Text other people wrote (messages, notes) comes back fenced as untrusted data, in a fence it can't close; shared memory and person notes refuse entries that read like commands to Claude, and memory also refuses anything that reads like a rule for it: a rule is an edit that lands on a branch I merge (or that I allow in my app's own prompt), so nothing a chat reads can quietly change how Claude behaves; API keys stay server-side; the HomeKit helper only answers the connector (a shared secret).
- **Machine-agnostic.** The server names whatever Mac it runs on; moving it from my laptop to the home server was a clone, a few launchd jobs and a new Tailscale name.

Machine setup (Homebrew, dotfiles, secrets) is in [mac-setup](https://github.com/nickjmorrow/mac-setup).
