"""Resolve an Engine instance without falling back to another MA identity."""
def resolve_runtime(data, instance_id=None):
    requested = str(instance_id or "").strip()
    primary = data.get("runtime")
    if not requested:
        if primary is not None and getattr(primary, "entries", True): return primary
        return next((runtime for runtime in data.get("instance_runtimes", {}).values() if runtime.entries), primary)
    runtimes = list(data.get("instance_runtimes", {}).values())
    if primary is not None and primary not in runtimes:
        runtimes.append(primary)
    for runtime in runtimes:
        if any(entry.instance_id == requested for entry in runtime.entries):
            return runtime
    raise ValueError("The requested HOMEii Engine instance is not loaded")
