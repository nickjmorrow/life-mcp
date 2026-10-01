# Hue Bridge formats (confirmed live 2026-09-26)

Tried on the real bridge with disabled automations and unrecalled scenes in one room.

## Automations (`POST /clip/v2/resource/behavior_instance`)

Body: `{"type": "behavior_instance", "script_id": <script>, "enabled": false, "metadata": {"name": …}, "configuration": …}` → HTTP 201.

| Script | `configuration` accepted |
| --- | --- |
| Schedule | `{"what": [{"group": G, "recall": {"rid": scene, "rtype": "scene"}}], "when_extended": {"recurrence_days": [...], "start_at": {"time_point": {"type": "time", "time": {"hour", "minute"}}, "transition": {"minutes": 10}}}, "where": [{"group": G}]}` |
| Schedule, lights off | same, with `"recall": {"rid": <off recipe>, "rtype": "recipe"}` and `"time_point": {"type": "sunset"}` |
| Basic wake up routine | `{"end_brightness": 100.0, "fade_in_duration": {"seconds": 1800}, "style": "sunrise", "when": {"recurrence_days": [...], "time_point": {...}}, "where": [{"group": G}]}` |
| Go to sleep routines | `{"end_state": "turn_off", "fade_out_duration": {"seconds": 1800}, "style": "basic", "when": {...}, "where": [{"group": G}]}` |
| Timers | `{"duration": {"seconds": 600}, "what": [{"group": G, "recall": scene or off recipe}], "where": [{"group": G}]}`. **`end_state` is rejected** (additionalProperties), and **`what` is required**. |

The off recipe is `caabcf9c-984b-4a2c-a97a-0ed61a299258`, which the Hue app's "Turn off everything " also uses, and it works for a single room. Code finds it from existing automations rather than hard-coding it.

## Scenes (`POST /clip/v2/resource/scene`)

Both `"effects_v2": {"action": {"effect": "candle"}}` and `"effects": {"effect": "candle"}` are accepted in a scene action and read back unchanged. We use `effects_v2`.
