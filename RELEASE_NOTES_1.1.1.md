# HOMEii Flow Engine 1.1.1

Stable patch release · 25 October 2026 · Companion: [HOMEii Music Flow 6.1.2](https://github.com/r11a/homeii-music-flow/releases/tag/v6.1.2).

## Search fix

Do not cache an empty external-provider search result for 30 seconds. Music Assistant can return an empty result at its soft timeout while continuing the provider search in the background. A subsequent user search can now retrieve those late results instead of repeatedly receiving the Engine's cached empty response. Successful results and library-only searches retain caching.

Hebrew queries remain unchanged. The regression test exercises an empty response followed by a successful Hebrew result and verifies successful-result reuse.

## Update

In HACS, update HOMEii Flow Engine to 1.1.1 and **restart Home Assistant** to load the Python changes. Then update the card to 6.1.2 and refresh dashboard clients. Keep your existing integration entry, credentials and configuration.

Manual installation: extract `homeii-flow-engine-1.1.1.zip`, copy the complete `custom_components/homeii_flow` directory into `/config/custom_components/`, and restart Home Assistant. Do not nest `custom_components` twice.

**Migrating from card 5.9.3 is a breaking change:** configure this separate Home Assistant integration before replacing the old card. It is an integration, not an app/add-on. See the [installation instructions](README.md#install-the-engine--start-here).

This fix does not bypass Spotify rate limits or repair unavailable providers.
