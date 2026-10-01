# Life Home feasibility spike (2026-09-27)

**Result: feasible.** A Mac Catalyst app signed with his developer team and the HomeKit entitlement
reads Apple Home on the Mac (authorization status 5 = determined + authorized).

HomePods and the Apple TV are not exposed to third-party HomeKit apps (use pyatv for those).

Signing lessons:
- HomeKit isn't offered for macOS app IDs. Register as an iOS app (`supportedDestinations: [iOS, macCatalyst]`,
  `DERIVE_MACCATALYST_PRODUCT_BUNDLE_IDENTIFIER: NO`), bundle id `com.nicholai.life.home`.
- `xcodebuild -allowProvisioningUpdates` enabled HomeKit on the App ID only via an **iOS** build
  (`-destination generic/platform=iOS`); after that the Mac Catalyst profile signs.
- Needed once: accept the latest Program License Agreement; register the Mac as a device
  (`-allowProvisioningDeviceRegistration`).

Not yet tested: running without a window / at login, and writes (scenes, characteristics).

## Background check (2026-09-27)
- With `LSUIElement` and the window closed 5 s after launch, the app keeps running and serving.
- `NWListener` bound to 127.0.0.1:8767 works in the sandbox with `com.apple.security.network.server`;
  `/health` → `{"authorization":5,"homes":1,"ok":true}`; the LAN address is refused (loopback only).

## Part B results (2026-09-27)

- HomeKit's cached characteristic values are empty (`nil`) until read, so `/home` reads every readable value of reachable accessories first (concurrently, capped at 4 s; ~0.5 s live).
- Built-in scenes can have no actions, and automations may show no actions to a third-party app: they act on things it can't see (likely HomePod media).
- The camera exposes several "Custom" characteristics and two "Volume"s (Speaker, Microphone); the tools hide Custom/Identify/Name and label repeated names by service.
- Camera snapshots are deferred: HomeKit shows them only in an on-screen `HMCameraView`.

## Shared secret (2026-10-01, not yet built or tested live)

- Any program on the Mac could call the loopback API, so every request now needs `X-Life-Home-Token`.
  The connector (`server/home_mcp.py`) makes `~/.config/life-mcp/home-token` (600) on first use; the
  app reads it on every request through the read-only sandbox exception
  `com.apple.security.temporary-exception.files.home-relative-path.read-only` for exactly that file.
  No TCC prompt: `~/.config` isn't a protected folder, unlike another app's container or an app-group
  container (macOS 15 asks before a program reads those).
- Without the file Life Home answers 503; with a wrong or missing header, 403.
- Deploy order: update the connector first (it sends the header; an old Life Home ignores it), then
  rebuild Life Home. If the exception doesn't let the sandboxed app read the file, `/health` answers
  503 "no shared token yet" while the file exists.
