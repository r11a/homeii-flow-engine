# HOMEii Flow Engine 1.0.2

September 27, 2026 · Stable maintenance release · Companion: [HOMEii Music Flow 6.0.2](https://github.com/r11a/homeii-music-flow/releases/tag/v6.0.2)

This release aligns the Engine package, installation documentation and release notes with card 6.0.2. **Runtime behavior is unchanged from Engine 1.0.1**, apart from the reported version. It introduces no storage migration or new backend API requirement. Card 6.0.2's fan, icons, fonts, gestures and artwork animations are implemented in the card.

## Fixes retained from 1.0.1

- Wait for Music Assistant WebSocket authentication before startup contract probes, avoiding false probe failures ([Engine #2](https://github.com/r11a/homeii-flow-engine/issues/2)).
- Keep playback statistics, connection diagnostics and screensaver recommendations stable when only regenerated timestamps differ.
- Coalesce Engine state dispatch and avoid forced Recorder updates and duplicated large sensor attributes.
- Preserve compact status/connection attributes and detailed on-demand diagnostics.

## Documentation and packaging

- Publish the 1.0.2 manifest, runtime version and Python project metadata as one matching package.
- Update the repository's stable pair, HACS/manual download links and beginner installation links to Engine 1.0.2 + card 6.0.2.
- Document [Engine #3](https://github.com/r11a/homeii-flow-engine/issues/3) accurately: the reported MQTT discovery error came from a stale/external retained message, not an Engine MQTT publisher. The Engine uses native HA entities/services and does not require MQTT. The issue's cleanup guidance remains available; this release does not delete MQTT messages.
- Include the complete `custom_components/homeii_flow` directory and a SHA-256 checksum for manual installation.

## Upgrade

1. Back up Home Assistant and the installed component.
2. Update Engine to 1.0.2 through HACS, or extract `homeii-flow-engine-1.0.2.zip` and replace `/config/custom_components/homeii_flow` with the complete component folder.
3. Restart Home Assistant and verify that the existing Engine entry loads and Music Assistant connects. Keep the entry and credentials; do not recreate them for a routine update.
4. Update card to 6.0.2 and fully reload browser/Companion clients.

**Upgrading from card 5.9.3 remains an Engine-first breaking migration.** The Engine requires HA 2025.1+ as declared in metadata, the official MA integration, MA API schema 63+, a reachable direct MA server and valid MA credentials configured server-side.

Existing Recorder history is not purged. No automatic changes are made to MQTT, speakers, groups, timers or schedules by this release procedure. Restoring an older card does not disable persistent Engine automations.

## Issue status and credits

- **Resolved previously:** [#2](https://github.com/r11a/homeii-flow-engine/issues/2), reported by [@wimjanse](https://github.com/wimjanse), remains covered by the authentication/probe fix.
- **Troubleshooting resolution:** [#3](https://github.com/r11a/homeii-flow-engine/issues/3), reported by [@MrAvana](https://github.com/MrAvana), documents stale external MQTT discovery; it is not listed as a new code fix.
- **Still open in the card tracker:** [card #96](https://github.com/r11a/homeii-music-flow/issues/96), reported by [@drshaw-lab](https://github.com/drshaw-lab), concerns permanent MA groups versus ad-hoc groups. This maintenance release does not resolve it.

Maintained by **Ronen Atsil / HOMEii**. Thanks to **Home Assistant, Music Assistant, HACS, Sendspin / Open Home Foundation**, community reporters and testers, and the card's translation contributors. Codex assisted with development and verification. This is an independent community integration.

## Validation

87 automated Engine tests pass. Repository/version validation and Python compilation are checked, and the distribution archive is verified against the component files. These checks do not certify every provider, speaker or HA installation.

[Card release notes](https://github.com/r11a/homeii-music-flow/releases/tag/v6.0.2) · [Installation and rollback](https://github.com/r11a/homeii-music-flow/blob/v6.0.2/docs/INSTALL_STEP_BY_STEP.md)
