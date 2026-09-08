"""Lightweight repository validation for HOMEii Flow Engine."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "homeii_flow"
COMMANDS = {
    "homeii_flow/bootstrap/get",
    "homeii_flow/get_context",
    "homeii_flow/diagnostics/run",
    "homeii_flow/stats/get",
    "homeii_flow/playback_stats/get",
    "homeii_flow/orchestration/status",
    "homeii_flow/orchestration/run_once",
    "homeii_flow/queue/get",
    "homeii_flow/queue/action",
    "homeii_flow/queue/transfer",
    "homeii_flow/library/get",
    "homeii_flow/search/get",
    "homeii_flow/players/get",
    "homeii_flow/playback/play_media",
    "homeii_flow/player/command",
    "homeii_flow/ma/command",
    "homeii_flow/favorites/get",
    "homeii_flow/favorites/set",
    "homeii_flow/group/apply",
    "homeii_flow/schedules/get",
    "homeii_flow/schedules/set",
    "homeii_flow/schedules/delete",
    "homeii_flow/schedules/run",
    "homeii_flow/timers/get",
    "homeii_flow/timers/set",
    "homeii_flow/timers/delete",
    "homeii_flow/volume_rules/get",
    "homeii_flow/volume_rules/set",
    "homeii_flow/volume_rules/delete",
    "homeii_flow/volume_rules/clear",
    "homeii_flow/announce",
    "homeii_flow/activity/get",
    "homeii_flow/screensaver/get",
    "homeii_flow/screensaver/set",
    "homeii_flow/screensaver/show",
    "homeii_flow/sendspin/status",
}


def load_json(path: Path) -> dict:
    """Load a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    """Validate repo structure."""
    component = ROOT / "custom_components" / DOMAIN
    required_files = [
        component / "__init__.py",
        component / "manifest.json",
        component / "config_flow.py",
        component / "runtime.py",
        component / "websocket_api.py",
        component / "diagnostics.py",
        component / "binary_sensor.py",
        component / "button.py",
        component / "calendar.py",
        component / "number.py",
        component / "sensor.py",
        component / "switch.py",
        component / "services.yaml",
        component / "icon.png",
        component / "logo.png",
        component / "frontend" / "homeii-flow-system-screensaver.js",
        component / "frontend" / "homeii-flow-icon.png",
        component / "frontend" / "homeii-flow-logo.png",
        component / "translations" / "en.json",
        ROOT / "icon.png",
        ROOT / "logo.png",
        ROOT / "hacs.json",
        ROOT / "README.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.exists()]
    if missing:
        raise SystemExit(f"Missing required files: {', '.join(missing)}")

    manifest = load_json(component / "manifest.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project.get("version") != manifest.get("version"):
        raise SystemExit("pyproject.toml and manifest.json versions must match")
    if manifest.get("domain") != DOMAIN:
        raise SystemExit("manifest.json domain does not match folder name")
    if not manifest.get("version"):
        raise SystemExit("manifest.json must include version for a custom integration")
    if manifest.get("version") != "0.7.21":
        raise SystemExit("manifest.json must expose the HOMEii Flow Engine 0.7.21 contract")
    if manifest.get("dependencies") != ["music_assistant"]:
        raise SystemExit("manifest.json must require the Music Assistant integration")
    if manifest.get("config_flow") is not True:
        raise SystemExit("manifest.json must enable config_flow")

    hacs = load_json(ROOT / "hacs.json")
    if DOMAIN not in hacs.get("domains", []):
        raise SystemExit("hacs.json must list the integration domain")

    ws_text = (component / "websocket_api.py").read_text(encoding="utf-8")
    missing_commands = sorted(command for command in COMMANDS if command not in ws_text)
    if missing_commands:
        raise SystemExit(f"Missing websocket commands: {', '.join(missing_commands)}")

    const_text = (component / "const.py").read_text(encoding="utf-8")
    runtime_text = (component / "runtime.py").read_text(encoding="utf-8")
    if 'VERSION = "0.7.21"' not in const_text:
        raise SystemExit("const.py and manifest.json versions must match")
    forbidden_runtime_paths = {
        "singular radio library command": '"radio": ["radio"]',
        "Home Assistant library fallback": 'async_call_service_response("music_assistant", "get_library"',
        "Home Assistant queue fallback": 'async_call_service_response("music_assistant", "get_queue"',
        "legacy mass_queue fallback": '"domain": "mass_queue"',
        "schedule media_play fallback": 'fallback_action',
    }
    for label, marker in forbidden_runtime_paths.items():
        if marker in runtime_text:
            raise SystemExit(f"Forbidden HOMEii Flow 6 runtime path remains: {label}")
    required_performance_contract = {
        "persistent media detail capability": '"persistent_media_detail_cache": True',
        "stable artwork capability": '"stable_artwork_urls": True',
        "artwork ETag capability": '"artwork_etag": True',
        "compatible shelf capability": '"compatible_library_shelves": True',
        "compact library capability": '"compact_library_responses": True',
        "revisioned snapshot capability": '"revisioned_snapshots": True',
        "active source capability": '"active_source_contract": True',
        "favorite mutation capability": '"favorite_mutation": True',
        "Music Assistant 2.10 capability": '"music_assistant_2_10": True',
        "Music Assistant schema 63 capability": '"music_assistant_schema_63": True',
        "Music Assistant WebSocket command capability": '"music_assistant_websocket_commands": True',
        "Music Assistant radio library path": '"radio": ["radios"]',
        "direct catalog capability": '"direct_library_catalog": True',
        "direct player capability": '"direct_player_catalog": True',
        "full queue capability": '"full_queue_snapshots": True',
        "queue autoplay capability": '"queue_autoplay": True',
        "detail request coalescing": "_media_command_inflight",
        "queue request coalescing": "_queue_inflight",
        "detail stale-while-revalidate cache": "_media_command_cache",
        "deterministic artwork tokens": "hashlib.blake2s",
        "compatible larger shelf reuse": "_library_cache_entry",
        "compact library responses": "_library_response",
        "snapshot epoch": "_snapshot_epoch",
        "stale response ordering": "_snapshot_meta",
        "server info handshake": "_async_music_assistant_server_info",
        "required contract probes": "_async_music_assistant_contract_probe",
        "native player identity map": "_ma_players_by_entity",
        "preferred API endpoint": "_ma_preferred_base_url",
        "provider discovery cache": "_provider_ids_cache",
    }
    for label, marker in required_performance_contract.items():
        source = const_text if "capability" in label else runtime_text
        if marker not in source:
            raise SystemExit(f"Missing 0.7.17 state/performance contract: {label}")

    print("HOMEii Flow Engine repo validation passed.")


if __name__ == "__main__":
    main()
