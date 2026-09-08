# HOMEii Flow Engine 1.0.0 Beta 1

**Draft release notes. No release or tag is published by this preparation.**

Matching card: **[HOMEii Music Flow 6.0.0-beta.1](https://github.com/r11a/homeii-music-flow)**. Install and configure the Engine before upgrading card 5.9.3. Read the [full setup guide](README.md) and [Hebrew migration warning](docs/BETA_UPGRADE_HE.md).

## What is included

Authenticated MA connection and events, player/active-queue mapping, paginated queue and library reads, search, artwork proxy, favorites, group operations, playback controls and HA services. Stored schedules, timers and volume rules work independently of an open card while HA is running. The integration exposes sensors, switches, buttons, numbers, binary sensors and a schedule calendar. Capability-dependent features include playback preferences, spoken-media speed, announcements, Sendspin relay and configured MA AI Radio DJ access.

## Required setup

The official MA HA integration must be loaded. MA API schema 63+ and a valid MA token are required; configure the actual server URL/port, not an HA ingress page. Optional features depend on MA plugins, provider/player support, TTS setup and browser permissions. The Engine is a custom integration, not an add-on or an MA replacement.

## Upgrade and recovery

This labels the preceding 0.7.21 development code as the first 1.0 beta; it does not intentionally reset the storage format. Back up existing component files and HA configuration/storage, retain the existing config entry, replace the complete component and restart HA. Verify both versions and one player before expanding. Restoring an old card does not stop Engine schedules: disable unwanted beta-created tasks and restore the appropriate HA backup when reverting backend changes.

## Known limits

Long-running grouping, specific DLNA hardware, mobile background audio, audible announcement restoration and broader recovery scenarios need continued testing. Use native MA as a comparison. Do not rely on this beta for critical alarms or guaranteed announcements. A beta is not a stable 1.0 release.

## Publication policy

Repository remains private until the owner explicitly decides otherwise. Public access or public packaging is a publication prerequisite. Planned releases must be Pre-release and not Latest. Users enabling beta updates or custom update automations control their own update policy; manual version selection is recommended for testing.

Report both component versions, HA/MA version/schema, player model/protocol, reproduction and redacted diagnostics in the [Engine tracker](https://github.com/r11a/homeii-flow-engine/issues). Never share tokens or full backups.
