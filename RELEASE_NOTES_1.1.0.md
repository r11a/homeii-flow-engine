# HOMEii Flow Engine 1.1.0

Stable release · 24 October 2026 · Companion: [HOMEii Music Flow 6.1.0](https://github.com/r11a/homeii-music-flow/releases/tag/v6.1.0).

## Installation and upgrade

**Install this Home Assistant integration before updating the card. It is not a Home Assistant app/add-on.** Back up HA first. In HACS, update HOMEii Flow Engine to 1.1.0, restart HA, and confirm Settings → Devices & services → HOMEii Flow Engine is loaded. Then update the card to 6.1.0 and refresh clients. Keep the existing integration entry and credentials.

Manual installation: download `homeii-flow-engine-1.1.0.zip`, extract it and copy the complete `custom_components/homeii_flow` directory into `/config/custom_components/`. Confirm `/config/custom_components/homeii_flow/manifest.json` shows 1.1.0. Restart HA. Do not nest `custom_components` twice or mix individual files from different versions. For a first installation, add HOMEii Flow Engine under Devices & services and configure the direct MA server URL and MA credentials using the guided setup. See the [README installation instructions](README.md#install-the-engine--start-here).

**BREAKING MIGRATION FROM CARD 5.9.3:** HOMEii 6 requires this integration; configure and verify it before replacing the old card. Use an MA token, not an HA token or Spotify API key. An HA ingress/sidebar URL is not the direct MA API address.

## Fixes

- Correct the duplicate library/get WebSocket schema discriminator.
- Dedicated permission-scoped MA event subscription for non-admin dashboard users; paired card update required.
- Separate clients, storage, caches, event/artwork and Sendspin routing per Engine config entry. Explicit missing instances no longer fall back to another connection; configured MA URL takes precedence over discovery.
- Native playlist playback without a blocking full-track prefetch. Latest pending request wins per player; already-executing MA requests cannot be cancelled remotely.
- Local MA artwork proxy preference and concurrent source/target checks for queue transfer.

## New capabilities

- Persistent listening scenes, managed from Smart → Scenes and runnable through `homeii_flow.run_scene`. Scenes validate players, report partial failures and do not blindly retry uncertain playback.
- Scheduled wake-volume fades (0–600 seconds), respecting volume limits and stopping on pause/manual volume changes.
- Temporary volume-rule suspension (0–1,440 minutes; 0 resumes immediately).
- Finite queue copy without stopping the source. Replaces the target, starts the current track from its beginning, does not synchronize speakers, and rejects dynamic/same-group/incomplete/over-2,000-item queues.
- Additional scene, lighting and activity diagnostics/sensors, plus supported MA statistics access.

## Multiple instances

Use distinct Engine Instance IDs for distinct connections and set each card's `homeii_engine_instance_id` accordingly. Default Profile ID separates HOMEii-owned saved settings; it is not an MA user selector. Provider visibility and permissions are still controlled by the configured MA identity. Existing installations are retained; separate instances do not automatically clone shared schedules and lighting rules, avoiding duplicate automation execution.

## Verification and limits

109 automated Engine tests passed. Local startup, context, scene persistence/profile isolation and native playback were checked. Two real MA accounts, physical grouping hardware and audible queue-copy/fade acceptance are not exhaustively verified. A fade in progress is cancelled by HA shutdown and is not resumed; the saved schedule remains. Always test automations on one intended speaker before enabling unattended use.

## Rollback

Reinstall the complete Engine 1.0.2 component and card 6.0.2, restart HA and restore the previous card resource. New scene/multi-instance/fade features are not supported by that pair. Restore your pre-upgrade HA backup for an exact return of storage and configuration; do not delete integration entries as a routine downgrade step.

## Thanks

Thanks to all issue reporters and community contributors. The companion card adds Dutch thanks to [Roy75 / Kobra](https://github.com/Roy75), [card PR #105](https://github.com/r11a/homeii-music-flow/pull/105).
