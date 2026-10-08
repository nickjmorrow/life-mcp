# hue

Claude tools for the Philips Hue lights at home. Talk to Claude on the phone
("dim the living room") and it changes the lights.

The life-mcp server (`../server`) mounts `hue_mcp.py` with namespace `hue`, because
claude.ai only connects on port 443 and Funnel's 443 already serves that server:

claude.ai → the connector's public URL (Tailscale Funnel) → `server/` on 127.0.0.1:8765
(GitHub OAuth, one allowed account) → these tools → the Hue Bridge on the LAN (CLIP API v2).

Tools (28), all `hue_*` on the Life connector:

- **Lights** (`hue_lights.py`): `list_home`, `set_room`, `set_light` (brightness, relative
  brightness, white temperature, colour, effects, gradients, fades), `all_off`,
  `identify_light`, `timed_effect` (sunrise/sunset), `signal`, `set_power_on`.
- **Scenes** (`hue_scenes.py`): `activate_scene` (dynamic, brightness, fade),
  `create_scene`, `update_scene`, `delete_scene`.
- **Automations** (`hue_automations.py`): `list_automations`, `set_automation_enabled`,
  `create_schedule`, `create_wake_up`, `create_go_to_sleep`, `create_timer`,
  `update_automation`, `delete_automation`.
- **Setup** (`hue_setup.py`): `rename`, `move_light`, `create_zone`, `update_zone`,
  `delete_zone`, `create_room`, `delete_room`, `status`.

Lights are named the way the Hue app shows them: the device's name. The light's own
name can be stale. `rename` updates both.

## Files

- `hue.py`: name matching, colours, request bodies, home model, and the bridge client (one `GET /resource` per call).
- `hue_common.py`: the FastMCP server and shared helpers. `hue_mcp.py`: the entry point that imports the tool modules.
  `uv run --project ../server hue_mcp.py --stdio` serves them locally with no auth.
- `pair.py`: creates the bridge app key.
- `docs/bridge-formats.md`: automation and scene formats confirmed on the real bridge.

## Running

Runs inside the life-mcp server (launchd `com.nicholai.life-mcp`, log
`~/Library/Logs/life-mcp.log`). After changing code here, restart it:
`launchctl kickstart -k gui/$(id -u)/com.nicholai.life-mcp`. If these tools fail
to import, the log says "Hue tools not loaded" and the rest of the connector keeps working.

## Secrets (`~/.zshrc.local`)

`HUE_BRIDGE_IP`, `HUE_APP_KEY`.

## Bridge

- New app key: delete the old `HUE_*` lines, press the bridge's button, then run
  `uv run --project ../server pair.py >> ~/.zshrc.local` (it waits up to 5 minutes).
- If the bridge's IP changes, run `uv run --project ../server pair.py --find`, update `HUE_BRIDGE_IP`, and
  restart. Better: reserve the bridge's IP in the router.
- Bulbs that are unplugged or out of range make the bridge warn "may not have effect".
  Those warnings don't count as failures.

## Test

```bash
uv run --project ../server pytest -q   # dependencies: ../server/pyproject.toml
```
