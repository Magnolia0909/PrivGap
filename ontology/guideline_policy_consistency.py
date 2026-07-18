from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from config.config import Config
from ontology.ontology_base import OntologyBase
from ontology.ontology_normalizer import OntologyNormalizer

@dataclass(frozen=True)
class PathRelation:
    relation: str
    guide_path: str
    policy_path: str

def compare_extraction_dirs(
    platform: str,
    guide_dir: str,
    policy_dir: str,
    use_ontology_alignment: bool = True,
    use_semantic_fallback: bool | None = None,
    use_controller_scope: bool = True,
    app_ids: Optional[Set[str]] = None,
) -> dict:
    cfg = _build_cfg(platform)
    if use_semantic_fallback is not None:
        cfg.use_semantic_ontology_fallback = use_semantic_fallback
    kb = OntologyBase(cfg)
    normalizer = OntologyNormalizer(kb)

    guide_items = _load_privacy_items_dir(_resolve_items_dir(guide_dir, "guide", ["guide"]))
    policy_items = _load_privacy_items_dir(_resolve_items_dir(policy_dir, "policy", ["policy"]))
    if use_controller_scope:
        policy_items = _select_controller_scope_policy_items(policy_items)
    selected_app_ids = sorted(app_ids if app_ids is not None else guide_items)
    app_results = []
    for app_id in selected_app_ids:
        if use_ontology_alignment:
            guide_map, guide_unmatched, _guide_norm = _normalize_items(guide_items.get(app_id, []), normalizer)
            policy_map, policy_unmatched, _policy_norm = _normalize_items(
                policy_items.get(app_id, []), normalizer, supplement_parent_fallback=True
            )
            policy_auxiliary_scope_map = _filter_outside_guideline_scope(policy_map)
            guide_map = _filter_to_guideline_scope(guide_map)
            policy_map = _filter_to_guideline_scope(policy_map)
        else:
            guide_map, guide_unmatched, _guide_norm = _fallback_items(guide_items.get(app_id, []), normalizer)
            policy_map, policy_unmatched, _policy_norm = _fallback_items(policy_items.get(app_id, []), normalizer)
            policy_auxiliary_scope_map = {}
        app_result = _build_app_result(
            platform, app_id, guide_map, policy_map, guide_unmatched, policy_unmatched, policy_auxiliary_scope_map
        )
        app_results.append(app_result)
    return {"app_results": app_results}

def _build_cfg(platform: str) -> Config:
    cfg = Config()
    cfg.source = platform
    cfg._apply_source_paths(platform)
    return cfg

def _default_guide_dir(cfg: Config) -> str:
    return os.path.join(cfg.RESULTS_ROOT, "entity_extraction", "guide","extractions")

def _default_policy_dir(cfg: Config) -> str:
    model = cfg.safe_path_part(cfg.get_llm_model_name())
    return os.path.join(cfg.RESULTS_ROOT, "entity_extraction", model, "extractions")

def _resolve_items_dir(root: str, kind: str, preferred_subdirs: List[str]) -> str:
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"{kind} directory does not exist: {root}")
    if _has_privacy_items(root_path):
        return root_path.as_posix()
    for subdir in preferred_subdirs:
        candidate = root_path / subdir
        if candidate.is_dir() and _has_privacy_items(candidate):
            return candidate.as_posix()
    subdirs = [p for p in root_path.iterdir() if p.is_dir()]
    matching = [p for p in subdirs if _has_privacy_items(p)]
    if len(matching) == 1:
        return matching[0].as_posix()
    raise FileNotFoundError(f"No *_privacy_items.json found for {kind}: {root}")

def _has_privacy_items(path: Path) -> bool:
    return any(p.name.endswith("_privacy_items.json") for p in path.iterdir() if p.is_file())

def _load_privacy_items_dir(dir_path: str) -> Dict[str, list]:
    items = {}
    for path in sorted(Path(dir_path).glob("*_privacy_items.json")):
        app_id = path.name[: -len("_privacy_items.json")]
        items[app_id] = json.loads(path.read_text(encoding="utf-8"))
    return items

def _select_controller_scope_policy_items(items_by_app: Dict[str, list]) -> Dict[str, list]:
    return {
        app_id: [item for item in items if _is_developer_policy_item(item)]
        for app_id, items in items_by_app.items()
    }

def _is_developer_policy_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    recipients = [
        str(recipient).strip()
        for recipient in item.get("recipients") or []
        if str(recipient).strip()
    ]
    if not recipients:
        return True
    if any(_has_hint(recipient, THIRD_PARTY_RECIPIENT_HINTS) for recipient in recipients):
        return False
    return all(_has_hint(recipient, FIRST_PARTY_RECIPIENT_HINTS) for recipient in recipients)

def _has_hint(text: str, hints: Tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)

def _normalize_items(
    items: list,
    normalizer: OntologyNormalizer,
    supplement_parent_fallback: bool = False,
) -> Tuple[Dict[str, List[str]], List[str], list]:
    path_to_terms: Dict[str, List[str]] = {}
    unmatched_terms: List[str] = []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dt = str(item.get("data_type") or "").strip()
        item_copy = dict(item)
        if not dt:
            item_copy["ontology_match"] = None
            normalized.append(item_copy)
            continue
        existing = item.get("ontology_match")
        if isinstance(existing, dict) and existing.get("hierarchy_path"):
            match = existing
        else:
            normalized_match = normalizer.normalize_data_type(dt)
            match = _match_to_dict(normalized_match) if normalized_match else None
        if match:
            path = match["hierarchy_path"]
            path_to_terms.setdefault(path, {"terms": [], "match": match})
            if dt not in path_to_terms[path]["terms"]:
                path_to_terms[path]["terms"].append(dt)
            item_copy["ontology_match"] = match
        elif supplement_parent_fallback:
            fallback_match = normalizer.normalize_to_top_level_supplement(dt)
            fallback = _match_to_dict(fallback_match) if fallback_match else None
            if fallback:
                path = fallback["hierarchy_path"]
                path_to_terms.setdefault(path, {"terms": [], "match": fallback})
                if dt not in path_to_terms[path]["terms"]:
                    path_to_terms[path]["terms"].append(dt)
                item_copy["ontology_match"] = fallback
            else:
                if dt not in unmatched_terms:
                    unmatched_terms.append(dt)
                item_copy["ontology_match"] = None
        else:
            if not normalizer.is_ignorable_data_type(dt) and dt not in unmatched_terms:
                unmatched_terms.append(dt)
            item_copy["ontology_match"] = None
        normalized.append(item_copy)
    return path_to_terms, unmatched_terms, normalized



def _filter_to_guideline_scope(source_map: Dict[str, dict]) -> Dict[str, dict]:
    return {
        path: value
        for path, value in source_map.items()
        if _in_guideline_scope(path, source_map)
    }

def _filter_outside_guideline_scope(source_map: Dict[str, dict]) -> Dict[str, dict]:
    return {
        path: value
        for path, value in source_map.items()
        if not _in_guideline_scope(path, source_map)
    }

def _fallback_items(items: list, normalizer: OntologyNormalizer) -> Tuple[Dict[str, List[str]], List[str], list]:
    path_to_terms: Dict[str, dict] = {}
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dt = str(item.get("data_type") or "").strip()
        item_copy = dict(item)
        if not dt:
            item_copy["ontology_match"] = None
            normalized.append(item_copy)
            continue
        match_obj = normalizer.normalize_data_type(dt)
        match = _match_to_dict(match_obj) if match_obj else {
            "matched_node": dt,
            "hierarchy_path": f"RAW::{dt}",
            "node_id": f"raw::{dt}",
            "legal_basis": "",
            "interface": [],
            "match_type": "raw_unmatched",
            "scope": "raw",
            "api_bound": False,
        }
        raw_path = f"RAW::{dt}"
        raw_match = dict(match)
        raw_match["hierarchy_path"] = raw_path
        raw_match["match_type"] = "raw_exact"
        path_to_terms.setdefault(raw_path, {"terms": [], "match": raw_match})
        if dt not in path_to_terms[raw_path]["terms"]:
            path_to_terms[raw_path]["terms"].append(dt)
        item_copy["ontology_match"] = raw_match
        normalized.append(item_copy)
    return path_to_terms, [], normalized

def _match_to_dict(match) -> dict:
    return {
        "matched_node": match.matched_node,
        "hierarchy_path": match.hierarchy_path,
        "node_id": match.node_id,
        "legal_basis": match.legal_basis,
        "interface": match.interface,
        "match_type": match.match_type,
        "scope": match.scope,
        "api_bound": match.api_bound,
    }

def _build_app_result(
    platform: str,
    app_id: str,
    guide_map: Dict[str, List[str]],
    policy_map: Dict[str, List[str]],
    guide_unmatched: List[str],
    policy_unmatched: List[str],
    policy_auxiliary_scope_map: Optional[Dict[str, dict]] = None,
) -> dict:
    guide_paths = set(guide_map)
    policy_paths = set(policy_map)
    relations = _build_path_alignment(guide_paths, policy_paths)
    covered_guide = {r.guide_path for r in relations}
    covered_policy = {r.policy_path for r in relations}
    exact = [r for r in relations if r.relation == "exact"]
    granularity = [r for r in relations if r.relation != "exact"]
    guide_uncovered = sorted(guide_paths - covered_guide)
    policy_uncovered = sorted(policy_paths - covered_policy)
    policy_auxiliary_scope_map = policy_auxiliary_scope_map or {}

    guideline_missing_policy_scope = [p for p in guide_uncovered if _in_guideline_scope(p, guide_map)]
    guideline_auxiliary_scope = [p for p in guide_uncovered if not _in_guideline_scope(p, guide_map)]
    policy_missing_guideline_scope = [p for p in policy_uncovered if _in_guideline_scope(p, policy_map)]
    policy_auxiliary_scope = sorted(policy_auxiliary_scope_map)

    status_info = _classify_consistency_status(
        policy_missing_guideline_scope=policy_missing_guideline_scope,
        policy_auxiliary_scope=policy_auxiliary_scope,
        guideline_missing_policy_scope=guideline_missing_policy_scope,
        guideline_auxiliary_scope=guideline_auxiliary_scope,
    )

    policy_missing_guideline_scope_items = _build_items(policy_missing_guideline_scope, policy_map, "policy_terms")
    policy_auxiliary_scope_items = _build_items(policy_auxiliary_scope, policy_auxiliary_scope_map, "policy_terms")
    guideline_missing_policy_scope_items = _build_items(guideline_missing_policy_scope, guide_map, "guide_terms")
    guideline_auxiliary_scope_items = _build_items(guideline_auxiliary_scope, guide_map, "guide_terms")
    policy_only_items = policy_missing_guideline_scope_items + policy_auxiliary_scope_items
    guide_only_items = guideline_missing_policy_scope_items + guideline_auxiliary_scope_items

    issues = []
    if not status_info["is_compliant"]:
        issues.extend(_with_issue_type("policy_missing_guideline_scope", policy_missing_guideline_scope_items))
        issues.extend(_with_issue_type("guide_only", guide_only_items))

    supplementary_alignment = {
        "exact_matches": [_relation_to_dict(r, guide_map, policy_map) for r in exact],
        "granularity_matches": [_relation_to_dict(r, guide_map, policy_map) for r in granularity],
        "scope_difference_items": policy_auxiliary_scope_items,
        "unmatched_terms": {"guide": guide_unmatched, "policy": policy_unmatched},
    }

    return {
        "app_id": app_id,
        "platform": platform,
        "consistency_status": status_info["consistency_status"],
        "risk_level": status_info["risk_level"],
        "is_compliant": status_info["is_compliant"],
        "consistency_taxonomy": "guideline_scope_supplement_risk",
        "statistics": {
            "guide_matched_types": len(guide_paths),
            "policy_matched_types": len(policy_paths),
            "exact_match_count": len(exact),
            "granularity_match_count": len(granularity),
            "policy_only_count": len(policy_missing_guideline_scope) + len(policy_auxiliary_scope),
            "policy_missing_guideline_scope_count": len(policy_missing_guideline_scope),
            "policy_auxiliary_scope_count": len(policy_auxiliary_scope),
            "guide_only_count": len(guide_uncovered),
            "guideline_missing_policy_scope_count": len(guideline_missing_policy_scope),
            "guideline_auxiliary_scope_count": len(guideline_auxiliary_scope),
            "guide_unmatched_count": len(guide_unmatched),
            "policy_unmatched_count": len(policy_unmatched),
        },
        "document_compliance": {
            "status": status_info["consistency_status"],
            "risk_level": status_info["risk_level"],
            "is_compliant": status_info["is_compliant"],
            "scope_difference": bool(policy_auxiliary_scope_items),
        },
        "exact_matches": supplementary_alignment["exact_matches"],
        "granularity_matches": supplementary_alignment["granularity_matches"],
        "policy_only_items": policy_only_items,
        "policy_missing_guideline_scope_items": policy_missing_guideline_scope_items,
        "policy_auxiliary_scope_items": policy_auxiliary_scope_items,
        "guide_only_items": guide_only_items,
        "guideline_missing_policy_scope_items": guideline_missing_policy_scope_items,
        "guideline_auxiliary_scope_items": guideline_auxiliary_scope_items,
        "issues": issues,
        "supplementary_alignment": supplementary_alignment,
        "unmatched_terms": supplementary_alignment["unmatched_terms"],
    }

def _classify_consistency_status(
    policy_missing_guideline_scope: List[str],
    policy_auxiliary_scope: List[str],
    guideline_missing_policy_scope: List[str],
    guideline_auxiliary_scope: List[str],
) -> dict:
    has_policy_only = bool(policy_missing_guideline_scope)
    has_guide_only = bool(guideline_missing_policy_scope or guideline_auxiliary_scope)
    if not has_policy_only and not has_guide_only:
        if policy_auxiliary_scope:
            return _status("scope_difference_only", "compliant", True)
        return _status("fully_consistent_or_granular", "compliant", True)
    if has_policy_only and has_guide_only:
        return _status("high_risk_bidirectional_guideline_scope", "high", False)
    if guideline_missing_policy_scope or guideline_auxiliary_scope:
        return _status("high_risk_guide_only", "high", False)
    if policy_missing_guideline_scope:
        return _status("medium_risk_policy_missing_guideline_scope", "medium", False)
    return _status("scope_difference_only", "compliant", True)


def _status(consistency_status: str, risk_level: str, is_compliant: bool) -> dict:
    return {
        "consistency_status": consistency_status,
        "risk_level": risk_level,
        "is_compliant": is_compliant,
    }

def _build_path_alignment(guide_paths: Iterable[str], policy_paths: Iterable[str]) -> List[PathRelation]:
    relations: List[PathRelation] = []
    for g in sorted(guide_paths):
        for p in sorted(policy_paths):
            rel = _align_ontology_paths(g, p)
            if rel:
                relations.append(PathRelation(rel, g, p))
    return relations

def _align_ontology_paths(path1: str, path2: str) -> Optional[str]:
    if path1 == path2:
        return "exact"
    p1 = path1.split(" > ")
    p2 = path2.split(" > ")
    if len(p1) < len(p2) and p2[: len(p1)] == p1:
        return "policy_finer_than_guideline"
    if len(p2) < len(p1) and p1[: len(p2)] == p2:
        return "guide_finer_than_policy"
    return None

def _scope(platform: str, path: str, source_map: Optional[Dict[str, dict]] = None) -> str:
    if source_map is not None:
        scope = _match(source_map, path).get("scope")
        if scope:
            return scope
    root = path.split(" > ", 1)[0]
    if root == SUPPLEMENT_ROOT:
        return "policy_supplement"
    if root in set(GUIDELINE_ROOT_BY_PLATFORM.values()):
        return "guideline_governed"
    return "unknown"

def _relation_to_dict(rel: PathRelation, guide_map: Dict[str, List[str]], policy_map: Dict[str, List[str]]) -> dict:
    return {
        "relation": rel.relation,
        "guide_path": rel.guide_path,
        "policy_path": rel.policy_path,
        "guide_terms": _terms(guide_map, rel.guide_path),
        "policy_terms": _terms(policy_map, rel.policy_path),
        "guide_scope": _scope("", rel.guide_path, guide_map),
        "policy_scope": _scope("", rel.policy_path, policy_map),
        "guide_api_bound": _api_bound(rel.guide_path, guide_map),
        "policy_api_bound": _api_bound(rel.policy_path, policy_map),
    }

def _build_items(paths: Iterable[str], source_map: Dict[str, dict], source_key: str) -> List[dict]:
    return [
        {
            "hierarchy_path": p,
            "scope": _scope("", p, source_map),
            "api_bound": _api_bound(p, source_map),
            source_key: _terms(source_map, p),
        }
        for p in sorted(paths)
    ]

def _terms(source_map: Dict[str, dict], path: str) -> List[str]:
    value = source_map.get(path, {})
    if isinstance(value, dict):
        return value.get("terms", [])
    return value or []

def _match(source_map: Dict[str, dict], path: str) -> dict:
    value = source_map.get(path, {})
    if isinstance(value, dict):
        return value.get("match", {}) or {}
    return {}

def _in_guideline_scope(path: str, source_map: Dict[str, dict]) -> bool:
    return _scope("", path, source_map) == "guideline_governed"

def _api_bound(path: str, source_map: Dict[str, dict]) -> bool:
    return bool(_match(source_map, path).get("api_bound", False))

def _with_issue_type(issue_type: str, items: Iterable[dict]) -> List[dict]:
    return [{"issue_type": issue_type, **item} for item in items]
