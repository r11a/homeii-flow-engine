# HOMEii Flow Engine

<p align="center">
  <img src="logo.png" alt="HOMEii Flow Engine" width="360">
</p>

HOMEii Flow Engine is the required Home Assistant backend integration for HOMEii Music Flow 6.

HOMEii Music Flow 6 treats the card as the premium visual interface and the Engine as the source of truth for backend state. The Engine owns queue/library proxying, artwork registration, grouping orchestration, schedules, statistics, volume policies, announcements, diagnostics, and Sendspin status.

## Current Scope

### 0.7.19 AI Radio DJ

The card can list the hosts already configured in MA, read the current queue DJ,
and explicitly enable or disable that DJ. The bridge allows only
`ai_radio/hosts/list`, `ai_radio/queue_dj/status` and `ai_radio/queue_dj/set`;
host authoring, model credentials and voice configuration remain in MA.
Opening the card page does not start generation. A successful setting change is
confirmed by rereading MA's queue DJ status. MA 2.11.0b1 exposes these commands;
servers without the plugin/configuration show an unavailable state.

### 0.7.18 playback services

`homeii_flow.player_command` now accepts `command: seek` with `seek_position`
in seconds, and `command: playback_speed` with `speed` from 0.5 to 3.0.
Both resolve the player's active MA queue, including group ownership.
Playback speed requires a current podcast episode or audiobook; ordinary music
and empty queues are rejected with a descriptive validation error.
These commands can be used by Home Assistant scripts and automations without
an open card. The card shows speed controls only for supported spoken media.

```yaml
action: homeii_flow.player_command
data:
  player: media_player.kitchen
  command: playback_speed
  speed: 1.25
```

Replace the example entity with the intended Music Assistant player. Live seek
was checked on MA 2.11.0b1; audible spoken-media speed verification remains part
of the release checklist.

Version `0.7.17` makes Music Assistant (API schema 63 or newer) a required Home Assistant dependency and the authoritative backend for HOMEii Flow. The Engine owns the authenticated server handshake, registry-backed native player identity map, typed library/search reads, verified full queue snapshots and mutations, playback commands, stable artwork proxy, favorites, provider discovery caching and revisioned SWR caches. The API token stays in the Home Assistant config entry and is never serialized to the browser.

Queue options are also available to Home Assistant automations through `homeii_flow.player_command`:

```yaml
action: homeii_flow.player_command
data:
  player: media_player.computer_2
  command: crossfade
  crossfade_enabled: true
```

Use `command: autoplay` with `autoplay_enabled: true` for MA Autoplay. Explicit `false` disables either option. The Engine resolves a grouped player's active queue before dispatch; these options are validated in `queue_controls.py`. Announcement playback uses HA's TTS media source and MA's native announcement/resume behavior. The current local regression suite contains 47 passing checks; physical audio and hardware-specific behavior require live testing.


Global Music Assistant playback defaults are exposed through an administrator service:

```yaml
action: homeii_flow.set_queue_settings
data:
  autoplay_enabled: true
  autoplay_mode: library
  smart_shuffle_enabled: enabled
```

These defaults affect all MA players. Only fields supported by the connected server can be changed; omitted values are preserved, and every save is verified by readback. The same validation powers the card's Playback preferences screen. Per-queue commands above remain separate.

It provides:

- Home Assistant config flow
- HOMEii Flow Engine WebSocket API
- card context and capability discovery
- diagnostics and statistics snapshots
- persistent library snapshots that remain available across Home Assistant restarts
- background library refreshes that never block a usable stale snapshot
- coalesced identical library requests and bounded startup cache warming
- persistent SWR detail caching for playlists, albums, artists, recommendations, history, and in-progress media
- deterministic same-origin artwork URLs with browser/server revalidation
- HA diagnostic/stat sensors
- HA switch entities for stored schedules, one-shot timers and volume rules, so each backend action owns a visible HA control
- HA buttons for refresh, orchestration, applying volume rules and running the next schedule on demand
- HA binary sensors for playing/grouped player state and active volume policies
- HA number entities for tuning each stored volume rule directly from Home Assistant
- recent Engine activity visibility for diagnostics, dashboards and support troubleshooting
- per-schedule `Run now` buttons for testing stored schedules from the integration page
- a Home Assistant calendar entity that exposes stored HOMEii schedules as upcoming events
- passive playback statistics and a system-wide screensaver agent foundation

## System-wide screensaver

HOMEii Flow Engine can serve a global dashboard screensaver agent:

```text
/homeii_flow/homeii-flow-system-screensaver.js
```

Add it as a Home Assistant Lovelace JavaScript module resource, then enable **System screensaver** on the HOMEii Flow Engine device page or call `homeii_flow.set_screensaver`.

This is intentionally separate from the card DOM. Once the resource is loaded, the screensaver can appear on any dashboard page, even when the HOMEii Music Flow card is not visible.

The integration also exposes:

- `Show system screensaver now` button entity for an immediate one-time open request
- `System screensaver timeout` number entity for changing the idle delay from the integration page
- `homeii_flow.show_screensaver` service for automations/scripts
- Now Playing display with live player artwork detection, Engine artwork fallback proxy, title, artist, album and player name when Music Assistant playback is active
- dynamic album-art backdrop for the system screensaver, using the loaded artwork as a soft blurred background
- system screensaver text cleanup that hides raw stream URLs and `Unknown` placeholders from the Now Playing title area
- artwork proxy now checks the Music Assistant queue response and tries Music Assistant imageproxy paths before falling back to Home Assistant paths
- playback statistics that refresh from the live player snapshot when HA reads the sensors, not only from the background tick

For non-dashboard Home Assistant pages, Lovelace resources may not be loaded by Home Assistant. In that case add it as a frontend extra module instead:

```yaml
frontend:
  extra_module_url:
    - /homeii_flow/homeii-flow-system-screensaver.js
```

Restart Home Assistant after changing `configuration.yaml`, then hard refresh the browser.
- lightweight backend orchestration for schedules and volume policies
- next-schedule and next-timer visibility with schedule/timer details on sensor attributes
- active-player visibility for backend helpers, dashboards and automations
- Music Assistant player snapshots for the card and diagnostics
- backend playback proxy for Music Assistant/media_player playback
- backend player command proxy for play/pause/stop/next/previous/volume/mute
- queue/library proxy hooks
- queue transfer proxy
- group apply hook
- stored schedules, timers and volume rules, exposed through WebSocket, services and the Configure flow
- announcement and Sendspin status placeholders

The integration is required for HOMEii Music Flow 6. The 6.x card intentionally has no frontend-only playback, queue, library, search or artwork fallback path. Users who do not want the Engine should remain on HOMEii Music Flow 5.9.x.

## Install Locally

Copy `custom_components/homeii_flow` into your Home Assistant `custom_components` folder, then restart Home Assistant.

After restart:

1. Open **Settings > Devices & services**.
2. Choose **Add Integration**.
3. Search for **HOMEii Flow Engine**.
4. Enter the preferred internal MA server URL, including its port (for example `http://192.168.1.10:8095`).
5. Optionally enter an external HTTPS MA Web Server/API URL as a fallback. Do not use the Home Assistant ingress page.
6. Paste a Music Assistant API token. The token is stored only in the HA config entry.
7. Add the default instance.
8. Keep the official **Music Assistant** Home Assistant integration installed and loaded. HOMEii uses it for HA entity discovery while the authenticated direct API supplies the complete queue, library and event stream.
9. Open HOMEii Music Flow diagnostics and confirm the Engine, command bridge and realtime event stream all report connected.

If the Engine was already configured, open **Settings > Devices & services > HOMEii Flow Engine > Configure > General settings**. Enter the URL and token there. Leaving the token field empty keeps the existing token.

## Card Configuration

In HOMEii Music Flow 6, keep the Engine mode on `Required`:

```yaml
type: custom:homeii-music-flow
homeii_engine_mode: required
```

If the Engine is not installed, loaded, or reachable through Home Assistant WebSocket, the card shows an Engine-required message instead of running through legacy browser-side paths.

## WebSocket API

The card talks to the integration through Home Assistant WebSocket commands:

- `homeii_flow/get_context`
- `homeii_flow/diagnostics/run`
- `homeii_flow/stats/get`
- `homeii_flow/players/get`
- `homeii_flow/orchestration/status`
- `homeii_flow/orchestration/run_once`
- `homeii_flow/playback/play_media`
- `homeii_flow/player/command`
- `homeii_flow/queue/get`
- `homeii_flow/queue/transfer`
- `homeii_flow/library/get`
- `homeii_flow/group/apply`
- `homeii_flow/schedules/get`
- `homeii_flow/schedules/set`
- `homeii_flow/schedules/delete`
- `homeii_flow/schedules/run`
- `homeii_flow/timers/get`
- `homeii_flow/timers/set`
- `homeii_flow/timers/delete`
- `homeii_flow/volume_rules/get`
- `homeii_flow/volume_rules/set`
- `homeii_flow/volume_rules/delete`
- `homeii_flow/volume_rules/clear`
- `homeii_flow/announce`
- `homeii_flow/sendspin/status`

## Home Assistant Entities

After adding the integration, Home Assistant creates diagnostic/stat sensors for:

- Engine status
- Music Assistant players total
- playing Music Assistant players
- grouped Music Assistant players
- Music Assistant players
- stored schedules
- next stored schedule time
- stored timers
- recent Engine activity
- stored volume rules

These are meant for dashboards, automations, and support diagnostics. HOMEii Music Flow 6.0.0 requires this integration and will not access Music Assistant directly from the browser.

## Services

Developer Tools > Services exposes:

- `homeii_flow.set_volume_rule`
- `homeii_flow.delete_volume_rule`
- `homeii_flow.clear_volume_rules`
- `homeii_flow.set_schedule`
- `homeii_flow.delete_schedule`
- `homeii_flow.run_schedule`
- `homeii_flow.set_timer`
- `homeii_flow.delete_timer`
- `homeii_flow.play_media`
- `homeii_flow.player_command`
- `homeii_flow.transfer_queue`
- `homeii_flow.announce`
- `homeii_flow.run_orchestration`

`run_schedule` is useful while testing because it runs one stored schedule immediately. `run_orchestration` runs one immediate schedule/volume-policy pass without waiting for the next 30-second Engine tick.

You can also open **Settings > Devices & services > HOMEii Flow Engine > Configure** to add/delete schedules, timers and volume rules from a guided menu. Player choices there are limited to Music Assistant players.

## Local Orchestration Test

1. Create a volume rule for a real media player with `max_volume` lower than its current volume.
2. Call `homeii_flow.run_orchestration`.
3. Confirm Home Assistant lowers that player's volume.
4. Check HOMEii Flow Engine diagnostics and confirm orchestration shows a recent `last_volume_action`.

Schedule execution is also available, but it should be tested with a harmless media item first. The Engine executes it through the authenticated Music Assistant 2.10 queue API; there is no browser or Home Assistant media-player fallback.

## Local Validation

From this repository:

```powershell
python -m compileall custom_components\homeii_flow
python scripts\validate_repo.py
```

The real runtime test is inside Home Assistant:

1. Install the integration locally.
2. Restart Home Assistant.
3. Add HOMEii Flow Engine from Devices & services.
4. Open HOMEii Music Flow diagnostics.
5. Confirm `HOMEii Flow Engine` is `OK`.
