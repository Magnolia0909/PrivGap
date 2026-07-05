                                                                                
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from flow_analysis.config import ENGINE_DIR, code_dir, static_cache_dir
from flow_analysis.csv_io import TaintChain
from flow_analysis.platform_spec import load_spec, sink_flow_type_map, sink_payload_key_map, suppressed_flow_patterns, semantic_sink_patterns, auto_ui_handler_patterns, ui_event_source_methods, core_sink_methods, storage_key_sensitive_local_storage, wrapper_terminal_mode, direct_file_upload_args, resource_sink_patterns

RUN_APP_JS = ENGINE_DIR / "run_app.js"
NODE_TIMEOUT_SECONDS = 45

def _force_rerun() -> bool:
    return os.getenv("FORCE_RERUN", "").strip().lower() in {"1", "true", "yes", "y"}

def _write_spec_file(platform: str) -> Path:
    spec = load_spec(platform)
    payload = {
        "platform": spec.platform,
        "apiPrefix": spec.api_prefix,
        "sourceApis": sorted(spec.source_apis),
        "sourceMethods": sorted(spec.source_methods),
        "sinkMethods": sorted(spec.sink_methods),
        "coreSinkMethods": core_sink_methods(),
        "flowTypeByMethod": sink_flow_type_map(platform),
        "sinkPayloadKeys": sink_payload_key_map(platform),
        "suppressedFlowPatterns": suppressed_flow_patterns(platform),
        "semanticSinkPatterns": semantic_sink_patterns(platform),
        "resourceSinkPatterns": resource_sink_patterns(platform),
        "autoUiHandlerPatterns": auto_ui_handler_patterns(platform),
        "uiEventSourceMethods": ui_event_source_methods(platform),
        "storageKeySensitiveLocalStorage": storage_key_sensitive_local_storage(platform),
        "wrapperTerminalMode": wrapper_terminal_mode(platform),
        "directFileUploadArgs": direct_file_upload_args(platform),
    }
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"flowspec_{platform}_", delete=False, encoding="utf-8"
    )
    json.dump(payload, fd)
    fd.close()
    return Path(fd.name)

def run_app(app_dir: Path, platform: str, spec_path: Path, cache_path: Path | None = None) -> tuple[list[dict], bool]:
                                          
    if cache_path is not None and cache_path.is_file() and not _force_rerun():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")), True
        except json.JSONDecodeError:
            pass
    try:
        proc = subprocess.run(
            ["node", str(RUN_APP_JS), str(app_dir), platform, str(spec_path)],
            capture_output=True, text=True, timeout=NODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"  [{app_dir.name}] TIMEOUT, skipping", flush=True)
        return [], False
    if proc.returncode != 0:
        print(f"  [{app_dir.name}] engine error: {proc.stderr.strip()[:300]}", flush=True)
        return [], False
    try:
        witnesses = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        print(f"  [{app_dir.name}] bad JSON output, skipping", flush=True)
        return [], False
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(witnesses), encoding="utf-8")
    return witnesses, False

def run_platform(
    platform: str,
    app_id: str | None = None,
    app_ids: set[str] | None = None,
    code_root: Path | str | None = None,
) -> list[TaintChain]:
    root = Path(code_root) if code_root is not None else code_dir(platform)
    if app_id:
        app_dirs = [root / app_id]
    elif app_ids is not None:
        app_dirs = [root / aid for aid in sorted(app_ids) if (root / aid).is_dir()]
    else:
        app_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    spec_path = _write_spec_file(platform)
    cache_dir = static_cache_dir(platform)
    chains: list[TaintChain] = []
    cache_hits = 0
    try:
        for i, app_dir in enumerate(app_dirs, 1):
            cache_path = cache_dir / f"{app_dir.name}.json"
            witnesses, from_cache = run_app(app_dir, platform, spec_path, cache_path=cache_path)
            cache_hits += int(from_cache)
            for w in witnesses:
                chains.append(TaintChain.from_witness(platform, app_dir.name, w))
            tag = "cached" if from_cache else "ran"
            print(f"  [{i}/{len(app_dirs)}] {app_dir.name[:40]:<40} {len(witnesses)} chains ({tag})", flush=True)
    finally:
        spec_path.unlink(missing_ok=True)
    print(f"[{platform}] static engine: {cache_hits}/{len(app_dirs)} apps served from cache "
          f"({cache_dir}); set FORCE_RERUN=1 to bypass.", flush=True)
    return chains
