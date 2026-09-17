# HOMEii Flow Engine 1.0.1

> [!CAUTION]
> **BREAKING CHANGE FOR HOMEii Music Flow 5.9.3 USERS:** HOMEii Music Flow 6.0.1 requires this Engine. Install and configure Engine 1.0.1 first, restart Home Assistant, verify the integration is healthy, and only then update the card. Do not install the card first.

## Fixed

- Music Assistant startup no longer reports a false probe failure when its WebSocket is still authenticating.
- Playback statistics, connection diagnostics and screensaver recommendations no longer change only because a timestamp was regenerated.
- Recorder-facing sensors no longer force updates or attach large duplicated runtime snapshots.
- Engine state dispatch is coalesced to reduce redundant Home Assistant state writes.
- Status and connection entities now expose compact, stable attributes suitable for Recorder.

These changes substantially reduce unnecessary database growth while preserving the detailed diagnostics available through the card and Engine commands.

## Upgrade

1. Update HOMEii Flow Engine to 1.0.1 through HACS or replace `/config/custom_components/homeii_flow` with the 1.0.1 package.
2. Restart Home Assistant.
3. Confirm the Engine entry is loaded and Music Assistant is connected.
4. Update HOMEii Music Flow card to 6.0.1 and fully reload the browser or Companion App.

Existing Recorder rows are not deleted automatically. If the database already grew significantly, use Home Assistant's supported Recorder purge controls after making a backup.

Compatibility: Home Assistant 2025.1+, Music Assistant API schema 63+, HOMEii Music Flow card 6.0.1.
