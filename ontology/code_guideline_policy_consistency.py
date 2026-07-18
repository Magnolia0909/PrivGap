from __future__ import annotations
import json
import re
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Set, Tuple
from ontology.ontology_base import OntologyBase
from ontology.ontology_normalizer import OntologyNormalizer
from ontology.guideline_policy_consistency import (
    _build_cfg,
    _select_controller_scope_policy_items,
    _load_privacy_items_dir,
    _normalize_items,
    _align_ontology_paths,
    _resolve_items_dir,
)

def compare_three_way(
    platform: str,
    code_dir: str,
    guide_dir: str,
    policy_dir: str,
    out_dir: str,
    use_semantic_fallback: bool | None = None,
    use_controller_scope: bool = True,
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
    code_items = _load_code_outputs(code_dir)
    app_ids = sorted(code_items)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    code_norm_dir = out / "code_normalized"
    code_norm_dir.mkdir(parents=True, exist_ok=True)
    per_app_dir = out / "per_app"
    per_app_dir.mkdir(parents=True, exist_ok=True)

    app_results: List[dict] = []
    print(f"[code-gp-consistency] platform={platform} apps={len(app_ids)}")
    for idx, app_id in enumerate(app_ids, 1):
        if idx <= 3 or idx % 50 == 0 or idx == len(app_ids):
            print(f"[code-gp-consistency] {idx}/{len(app_ids)} {app_id}")

        raw_guide_items = guide_items.get(app_id, [])
        raw_policy_items = policy_items.get(app_id, [])
        guide_map, guide_unmatched, _guide_norm = _normalize_items(raw_guide_items, normalizer)
        policy_map, policy_unmatched, _policy_norm = _normalize_items(
            raw_policy_items, normalizer, supplement_parent_fallback=True
        )
        code_map, code_unknown = _code_map(code_items.get(app_id, {}), normalizer)
        _write_json(code_norm_dir / f"{app_id}_code_normalized.json", {
            "app_id": app_id,
            "code_nodes": _items_from_map(code_map, "code_terms"),
            "unknown_flows": code_unknown,
        })

        app_result = _build_three_way_result(
            platform=platform,
            app_id=app_id,
            code_map=code_map,
            guide_map=guide_map,
            policy_map=policy_map,
            code_unknown=code_unknown,
            guide_unmatched=guide_unmatched,
            policy_unmatched=policy_unmatched,
            guide_breadth=_raw_item_breadth(raw_guide_items),
            policy_breadth=_raw_item_breadth(raw_policy_items),
        )
        app_results.append(app_result)
        _write_json(per_app_dir / f"{app_id}.json", {
            "app_id": app_id,
            "platform": platform,
            "closer_to": app_result["closer_to"]["api_scope"],
            "coverage": app_result["coverage"],
            "disclosure_gap": app_result["disclosure_gap"],
            "guideline_permissions": app_result["guide_nodes"],
            "policy_permissions": app_result["policy_nodes"],
            "code_permissions": app_result["code_nodes"],
        })

    summary = _build_global_summary(platform, app_results)
    _write_json(out / "code_guideline_policy_app_results.json", app_results)
    _write_json(out / "code_guideline_policy_global_summary.json", summary)
    return {"app_results": app_results, "global_summary": summary}

def _load_code_outputs(code_dir: str) -> Dict[str, dict]:
    root = Path(code_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"code flow final directory does not exist: {code_dir}")
    result = {}
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        app_id = data.get("app_name") or path.stem
        flow_root = root.parent.parent if root.parent.name == "final" else root.parent
        staged_root = flow_root / "staged" / app_id
        if staged_root.is_dir():
            data["_staged_source_root"] = staged_root.as_posix()
        result[app_id] = data
    return result

def _code_map(code_output: dict, normalizer: OntologyNormalizer) -> Tuple[Dict[str, dict], List[dict]]:
    path_to_info: Dict[str, dict] = {}
    unknown: List[dict] = []
    source_root = str(code_output.get("_staged_source_root") or "")
    for flow in code_output.get("flows", []) or []:
        if not isinstance(flow, dict):
            continue
        raw_path = str(flow.get("ontology_hierarchy") or "").strip()
        node = str(flow.get("ontology_node") or "").strip()
        node_id = str(flow.get("ontology_node_id") or "").strip()
        if not raw_path or not node or node == "unknown" or node_id == "unknown":
            unknown.append(_unknown_flow(flow))
            recipient_type = flow.get("recipient_type") or (((flow.get("sink") or {}).get("receiver") or {}).get("recipient_type"))
            for term, signal_api, signal_file, signal_line in _code_side_inferred_terms(flow, source_root):
                _add_inferred_code_node(
                    path_to_info,
                    normalizer,
                    term=term,
                    signal_api=signal_api,
                    source_file=signal_file,
                    source_line=signal_line,
                    recipient_type=recipient_type,
                )
            continue

        normalized = normalizer.normalize_data_type(node)
        if normalized:
            path = normalized.hierarchy_path
            normalized_node_id = normalized.node_id
            scope = normalized.scope
            match_type = f"code_flow+{normalized.match_type}"
        else:
            path = raw_path
            normalized_node_id = node_id
            scope = "guideline_governed" if flow.get("is_official_api") else "policy_supplement"
            match_type = "code_flow"

        info = path_to_info.setdefault(path, {
            "terms": [],
            "match": {
                "matched_node": node,
                "hierarchy_path": path,
                "node_id": normalized_node_id,
                "api_bound": bool(flow.get("is_official_api")),
                "scope": scope,
                "match_type": match_type,
            },
            "flow_count": 0,
            "detected_by": set(),
            "source_apis": set(),
            "recipients": set(),
        })
        if node and node not in info["terms"]:
            info["terms"].append(node)
        info["flow_count"] += 1
        if flow.get("detected_by"):
            info["detected_by"].add(flow.get("detected_by"))
        src = flow.get("source") or {}
        if src.get("handler"):
            info["source_apis"].add(src.get("handler"))
        recipient_type = flow.get("recipient_type") or (((flow.get("sink") or {}).get("receiver") or {}).get("recipient_type"))
        if recipient_type:
            info["recipients"].add(recipient_type)

        for term, signal_api, signal_file, signal_line in _code_side_inferred_terms(flow, source_root):
            _add_inferred_code_node(
                path_to_info,
                normalizer,
                term=term,
                signal_api=signal_api,
                source_file=signal_file,
                source_line=signal_line,
                recipient_type=recipient_type,
            )

    for info in path_to_info.values():
        info["detected_by"] = sorted(info["detected_by"])
        info["source_apis"] = sorted(info["source_apis"])
        if isinstance(info.get("source_files"), set):
            info["source_files"] = sorted(info["source_files"])
        info["recipients"] = sorted(info["recipients"])
    return path_to_info, unknown

_PAYMENT_API_RE = re.compile(r"(^|\.)(payment|requestpayment|pay|wxpay|alipay)$", re.IGNORECASE)
_SYSTEM_API_RE = re.compile(r"(^|\.)(getsystem|getsysteminfo|getsysteminfosync|getdeviceinfo|getwindowinfo|getappbaseinfo)$", re.IGNORECASE)
_NETWORK_API_RE = re.compile(r"(^|\.)(getnetworktype|getconnectedwifi|getwifilist)$", re.IGNORECASE)

def _code_side_inferred_terms(flow: dict, source_root: str = "") -> List[Tuple[str, str, str, int]]:
    src = flow.get("source") or {}
    sink = flow.get("sink") or {}
    candidates = [
        (str(src.get("handler") or ""), str(src.get("file") or ""), int(src.get("loc_line") or 0), "source"),
        (str(sink.get("api") or ""), str(sink.get("file") or ""), int(sink.get("loc_line") or 0), "sink"),
    ]
    inferred: List[Tuple[str, str, str, int]] = []
    seen: Set[Tuple[str, str]] = set()
    for api, file_name, line, role in candidates:
        if not api:
            continue
        signal = f"{role}:{api}"
        terms: List[str] = []
        context = _nearby_code_context(source_root, file_name, line)
        if _PAYMENT_API_RE.search(api) or _looks_like_payment_context(context):
            signal = f"{signal}|context:payment" if not _PAYMENT_API_RE.search(api) else signal
            terms.extend(["订单信息", "支付信息"])
        if _SYSTEM_API_RE.search(api):
            terms.append("设备信息")
        if _NETWORK_API_RE.search(api):
            terms.extend(["网络连接信息", "网络配置"])
        for term in terms:
            key = (term, signal)
            if key in seen:
                continue
            seen.add(key)
            inferred.append((term, signal, file_name, line))
    return inferred

def _nearby_code_context(source_root: str, file_name: str, line: int, radius: int = 14) -> str:
    if not source_root or not file_name or line <= 0:
        return ""
    path = Path(source_root) / file_name
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "\n".join(lines[start:end])

def _looks_like_payment_context(context: str) -> bool:
    if not context:
        return False
    return bool(re.search(
        r"payment\s*:\s*function|function\s+payment\b|requestPayment\s*\(|/api/pay/pay|paySign|orderInfo\s*:|paytype|provider\s*:\s*['\"](?:wxpay|alipay)",
        context,
        re.IGNORECASE,
    ))

def _add_inferred_code_node(
    path_to_info: Dict[str, dict],
    normalizer: OntologyNormalizer,
    term: str,
    signal_api: str,
    source_file: str,
    source_line: int,
    recipient_type: str | None,
) -> None:
    normalized = normalizer.normalize_data_type(term)
    if not normalized:
        return
    path = normalized.hierarchy_path
    info = path_to_info.setdefault(path, {
        "terms": [],
        "match": {
            "matched_node": normalized.matched_node,
            "hierarchy_path": path,
            "node_id": normalized.node_id,
            "api_bound": bool(normalized.api_bound),
            "scope": normalized.scope,
            "match_type": f"code_side_inference+{normalized.match_type}",
        },
        "flow_count": 0,
        "detected_by": set(),
        "source_apis": set(),
        "recipients": set(),
    })
    if term not in info["terms"]:
        info["terms"].append(term)
    info["flow_count"] += 1
    info["detected_by"].add("code_side_inference")
    info["source_apis"].add(signal_api)
    if source_file:
        info.setdefault("source_files", set()).add(f"{source_file}:{source_line}" if source_line else source_file)
    if recipient_type:
        info["recipients"].add(recipient_type)

def _unknown_flow(flow: dict) -> dict:
    src = flow.get("source") or {}
    sink = flow.get("sink") or {}
    return {
        "source_handler": src.get("handler", ""),
        "source_file": src.get("file", ""),
        "source_line": src.get("loc_line", 0),
        "sink_api": sink.get("api", ""),
        "detected_by": flow.get("detected_by", ""),
    }

def _build_three_way_result(
    platform: str,
    app_id: str,
    code_map: Dict[str, dict],
    guide_map: Dict[str, dict],
    policy_map: Dict[str, dict],
    code_unknown: List[dict],
    guide_unmatched: List[str],
    policy_unmatched: List[str],
    guide_breadth: dict | None = None,
    policy_breadth: dict | None = None,
) -> dict:
    code_paths = set(code_map)
    code_api_paths = {p for p in code_paths if _guideline_governed(code_map, p)}
    code_supp_paths = code_paths - code_api_paths
    guide_paths = set(guide_map)
    policy_paths = set(policy_map)

    code_api_by_guide = _covered(code_api_paths, guide_paths)
    code_api_by_policy = _covered(code_api_paths, policy_paths)
    code_all_by_guide = _covered(code_paths, guide_paths)
    code_all_by_policy = _covered(code_paths, policy_paths)
    code_supp_by_policy = _covered(code_supp_paths, policy_paths)
    weighted = _weighted_coverage_summary(code_map, guide_paths, policy_paths)
    semantic = _semantic_coverage_summary(code_map, guide_paths, policy_paths)

    coverage = {
        "code_api_by_guideline": _coverage_ratio(code_api_by_guide, code_api_paths),
        "code_api_by_policy": _coverage_ratio(code_api_by_policy, code_api_paths),
        "code_all_by_guideline": _coverage_ratio(code_all_by_guide, code_paths),
        "code_all_by_policy": _coverage_ratio(code_all_by_policy, code_paths),
        "code_supplement_by_policy": _coverage_ratio(code_supp_by_policy, code_supp_paths),
        "weighted_by_guideline": weighted["guideline_score"],
        "weighted_by_policy": weighted["policy_score"],
        "weighted_total": weighted["total_weight"],
        "weighted_effective_nodes": weighted["effective_nodes"],
        "semantic_by_guideline": semantic["guideline_score"],
        "semantic_by_policy": semantic["policy_score"],
        "semantic_total": semantic["total_weight"],
        "semantic_effective_categories": len(semantic["code_core_categories"]),
    }
    closer, closer_basis = _closer_label(
        coverage["semantic_by_guideline"],
        coverage["semantic_by_policy"],
        code_api_paths,
        coverage["code_all_by_guideline"],
        coverage["code_all_by_policy"],
        code_paths,
        len(semantic["code_core_categories"]),
        semantic,
    )

    breadth_closer, breadth_basis = _breadth_sensitive_closer(
        base_label=closer,
        base_basis=closer_basis,
        guide_paths=guide_paths,
        policy_paths=policy_paths,
        code_paths=code_paths,
        guide_breadth=guide_breadth or {},
        policy_breadth=policy_breadth or {},
    )

    missing_api_in_guide = sorted(code_api_paths - set(code_api_by_guide))
    missing_api_in_policy = sorted(code_api_paths - set(code_api_by_policy))
    missing_supp_in_policy = sorted(code_supp_paths - set(code_supp_by_policy))
    declared_guide_not_observed = sorted(p for p in guide_paths if not _is_covered_by(p, code_api_paths))
    declared_policy_not_observed = sorted(p for p in policy_paths if not _is_covered_by(p, code_paths))
    disclosure_gap = _disclosure_gap_summary(
        missing_api_in_guide=missing_api_in_guide,
        missing_api_in_policy=missing_api_in_policy,
        missing_supp_in_policy=missing_supp_in_policy,
        declared_guide_not_observed=declared_guide_not_observed,
        declared_policy_not_observed=declared_policy_not_observed,
    )

    return {
        "app_id": app_id,
        "platform": platform,
        "coverage": coverage,
        "closer_to": {
            "api_scope": closer,
            "basis": closer_basis,
            "api_scope_with_breadth": breadth_closer,
            "breadth_basis": breadth_basis,
        },
        "disclosure_gap": disclosure_gap,
        "statistics": {
            "code_nodes": len(code_paths),
            "code_api_nodes": len(code_api_paths),
            "code_supplement_nodes": len(code_supp_paths),
            "guide_nodes": len(guide_paths),
            "policy_nodes": len(policy_paths),
            "guide_raw_items": (guide_breadth or {}).get("raw_items", 0),
            "policy_raw_items": (policy_breadth or {}).get("raw_items", 0),
            "guide_unique_raw_data_types": (guide_breadth or {}).get("unique_data_types", 0),
            "policy_unique_raw_data_types": (policy_breadth or {}).get("unique_data_types", 0),
            "code_unknown_flows": len(code_unknown),
            "guide_unmatched_terms": len(guide_unmatched),
            "policy_unmatched_terms": len(policy_unmatched),
            "code_api_missing_in_guideline": len(missing_api_in_guide),
            "code_api_missing_in_policy": len(missing_api_in_policy),
            "code_supplement_missing_in_policy": len(missing_supp_in_policy),
            "declared_guideline_not_observed": len(declared_guide_not_observed),
            "declared_policy_not_observed": len(declared_policy_not_observed),
        },
        "code_nodes": _items_from_map(code_map, "code_terms"),
        "guide_nodes": _items_from_map(guide_map, "guide_terms"),
        "policy_nodes": _items_from_map(policy_map, "policy_terms"),
        "evidence": {
            "code_api_missing_in_guideline": _items_from_paths(missing_api_in_guide, code_map, "code_terms"),
            "code_api_missing_in_policy": _items_from_paths(missing_api_in_policy, code_map, "code_terms"),
            "code_supplement_missing_in_policy": _items_from_paths(missing_supp_in_policy, code_map, "code_terms"),
            "declared_guideline_not_observed": _items_from_paths(declared_guide_not_observed, guide_map, "guide_terms"),
            "declared_policy_not_observed": _items_from_paths(declared_policy_not_observed, policy_map, "policy_terms"),
            "code_unknown_flows": code_unknown,
            "unmatched_terms": {"guide": guide_unmatched, "policy": policy_unmatched},
        },
    }

def _raw_item_breadth(items: list) -> dict:
    data_types = set()
    raw_count = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        dt = str(item.get("data_type") or "").strip()
        if not dt:
            continue
        raw_count += 1
        data_types.add(dt)
    return {"raw_items": raw_count, "unique_data_types": len(data_types)}

def _breadth_sensitive_closer(
    base_label: str,
    base_basis: str,
    guide_paths: Set[str],
    policy_paths: Set[str],
    code_paths: Set[str],
    guide_breadth: dict,
    policy_breadth: dict,
) -> Tuple[str, str]:
    if base_label != "tie":
        return base_label, base_basis + "; breadth tie-break not applied because base label is decisive"
    guide_extra_nodes = len([p for p in guide_paths if not _is_covered_by(p, code_paths)])
    policy_extra_nodes = len([p for p in policy_paths if not _is_covered_by(p, code_paths)])
    guide_raw = int(guide_breadth.get("unique_data_types", 0) or 0)
    policy_raw = int(policy_breadth.get("unique_data_types", 0) or 0)
    guide_breadth_score = guide_extra_nodes + guide_raw
    policy_breadth_score = policy_extra_nodes + policy_raw
    basis = (
        base_basis
        + "; breadth tie-break compares document-only normalized nodes and unique raw data types "
        + f"(guideline={guide_breadth_score}, policy={policy_breadth_score})"
    )
    diff = abs(policy_breadth_score - guide_breadth_score)
    smaller = max(1, min(policy_breadth_score, guide_breadth_score))
    if diff < BREADTH_TIEBREAK_MIN_EXTRA or max(policy_breadth_score, guide_breadth_score) / smaller < BREADTH_TIEBREAK_RATIO:
        return "tie", basis + "; breadth difference is too small"
    if policy_breadth_score > guide_breadth_score:
        return "policy", basis + "; policy discloses broader extra data types"
    return "guideline", basis + "; guideline discloses broader extra data types"

def _covered(source_paths: Iterable[str], target_paths: Set[str]) -> List[str]:
    return sorted(p for p in source_paths if _is_covered_by(p, target_paths))

def _is_covered_by(path: str, target_paths: Set[str]) -> bool:
    return any(_align_ontology_paths(path, target) for target in target_paths)

def _coverage_ratio(covered: Iterable[str], denominator: Set[str]) -> Optional[float]:
    if not denominator:
        return None
    return round(len(list(covered)) / len(denominator), 4)

def _weighted_coverage_summary(
    code_map: Dict[str, dict],
    guide_paths: Set[str],
    policy_paths: Set[str],
) -> dict:
    total = 0.0
    guide = 0.0
    policy = 0.0
    effective_nodes = 0
    node_weights = {}
    for path, info in code_map.items():
        weight = _node_weight(path, info)
        node_weights[path] = weight
        total += weight
        if weight >= USER_INPUT_SCOPE_WEIGHT:
            effective_nodes += 1
        if _is_covered_by(path, guide_paths):
            guide += weight
        if _is_covered_by(path, policy_paths):
            policy += weight
    return {
        "guideline_score": round(guide / total, 4) if total else None,
        "policy_score": round(policy / total, 4) if total else None,
        "total_weight": round(total, 4),
        "effective_nodes": effective_nodes,
        "node_weights": node_weights,
    }

def _node_weight(path: str, info: dict) -> float:
    if _guideline_governed({path: info}, path):
        return API_SCOPE_WEIGHT
    category = _ontology_super_category(path)
    if category in {"weak_auxiliary", "unknown"}:
        return AUXILIARY_SCOPE_WEIGHT
    return USER_INPUT_SCOPE_WEIGHT

def _semantic_coverage_summary(
    code_map: Dict[str, dict],
    guide_paths: Set[str],
    policy_paths: Set[str],
) -> dict:
    guide_categories = _categories_for_paths(guide_paths)
    policy_categories = _categories_for_paths(policy_paths)
    total = 0.0
    guide = 0.0
    policy = 0.0
    code_categories = set()
    code_core_categories = set()
    node_categories = {}
    for path, info in code_map.items():
        category = _ontology_super_category(path)
        node_categories[path] = category
        weight = _semantic_node_weight(path, info, category)
        total += weight
        if category != "unknown":
            code_categories.add(category)
        if weight >= USER_INPUT_SCOPE_WEIGHT and category not in {"weak_auxiliary", "unknown"}:
            code_core_categories.add(category)
        if category in guide_categories or _is_covered_by(path, guide_paths):
            guide += weight
        if category in policy_categories or _is_covered_by(path, policy_paths):
            policy += weight
    return {
        "guideline_score": round(guide / total, 4) if total else None,
        "policy_score": round(policy / total, 4) if total else None,
        "total_weight": round(total, 4),
        "code_categories": sorted(code_categories),
        "code_core_categories": sorted(code_core_categories),
        "guide_categories": sorted(guide_categories),
        "policy_categories": sorted(policy_categories),
        "node_categories": node_categories,
    }

def _categories_for_paths(paths: Iterable[str]) -> Set[str]:
    return {cat for cat in (_ontology_super_category(path) for path in paths) if cat != "unknown"}

def _semantic_node_weight(path: str, info: dict, category: str) -> float:
    if _guideline_governed({path: info}, path):
        return API_SCOPE_WEIGHT
    if category in {"weak_auxiliary", "unknown"}:
        return AUXILIARY_SCOPE_WEIGHT
    return USER_INPUT_SCOPE_WEIGHT

def _ontology_super_category(path: str) -> str:
    text = str(path or "").lower()
    checks = (
        ("phone", ("手机", "电话号码", "联系电话", "移动电话", "phone")),
        ("location", ("位置", "地理", "定位", "gps", "经纬", "地址位置")),
        ("media", ("相册", "照片", "图片", "图像", "视频", "影像", "影音", "摄像头", "相机", "camera")),
        ("account", ("账号", "账户", "openid", "open id", "unionid", "userid", "user id", "昵称", "头像", "登录名", "公开信息", "用户资料", "用户个人信息")),
        ("identity", ("身份", "实名", "证件", "身份证", "人脸", "姓名", "银行卡", "学历", "驾驶证", "营业执照")),
        ("payment_order", ("支付", "交易", "订单", "金额", "财务", "发票", "配送", "取件", "寄件", "乘机人")),
        ("address", ("通讯地址", "收货地址", "详细地址", "地址")),
        ("device_network", ("设备", "系统", "网络", "wifi", "wi-fi", "wlan", "ip", "imei", "oaid", "androidid", "mac", "日志", "运营商", "机型", "硬件", "软件")),
        ("contact", ("联系人", "通讯录", "通话", "短信")),
        ("content", ("发布内容", "投诉详情", "聊天", "评论", "文件", "剪切板", "剪贴板")),
        ("weak_auxiliary", ("存储数据", "缓存", "cookie", "cookies", "使用行为", "浏览", "启动", "入口", "性能")),
    )
    for category, terms in checks:
        if any(term in text for term in terms):
            return category
    return "unknown"

def _closer_label(
    guide_score: Optional[float],
    policy_score: Optional[float],
    code_api_paths: Set[str],
    fallback_guide_score: Optional[float],
    fallback_policy_score: Optional[float],
    code_paths: Set[str],
    effective_nodes: int = 0,
    semantic: Optional[dict] = None,
    tie_threshold: float = DEFAULT_CLOSER_TIE_THRESHOLD,
) -> Tuple[str, str]:
    if not code_paths:
        return "tie", "no classifiable code ontology nodes"
    if not code_api_paths and len(code_paths) <= 1 and effective_nodes == 0:
        return "tie", "single supplement node with unknown ontology category; insufficient evidence for closer determination"
    g = guide_score if guide_score is not None else (fallback_guide_score or 0.0)
    p = policy_score if policy_score is not None else (fallback_policy_score or 0.0)
    basis = (
        "semantic-profile coverage over code-relevant ontology categories "
        f"with api_scope_weight={API_SCOPE_WEIGHT}, user_input_scope_weight={USER_INPUT_SCOPE_WEIGHT}, "
        f"auxiliary_scope_weight={AUXILIARY_SCOPE_WEIGHT}, tie_threshold={tie_threshold}"
    )
    if semantic:
        core = set(semantic.get("code_core_categories") or [])
        guide_core = core & set(semantic.get("guide_categories") or [])
        policy_core = core & set(semantic.get("policy_categories") or [])
        policy_advantage = len(policy_core - guide_core)
        guide_advantage = len(guide_core - policy_core)
        if policy_advantage > guide_advantage:
            return "policy", basis + "; policy has broader coverage over code core categories"
        if guide_advantage > policy_advantage:
            return "guideline", basis + "; guideline has broader coverage over code core categories"
        if "device_network" in core and "device_network" in policy_core and "device_network" not in guide_core and (p - g) >= tie_threshold:
            return "policy", basis + "; device/log/network flow is covered by policy but not guideline and policy coverage is higher"
        if core and guide_core == policy_core and abs(g - p) < tie_threshold:
            return "tie", basis + "; both documents cover the same code core categories with close coverage"
    if effective_nodes <= 1 and g > 0 and p > 0 and abs(g - p) < tie_threshold:
        return "tie", basis + "; single effective node covered by both documents with close coverage"
    if abs(g - p) < tie_threshold:
        return "tie", basis
    if g > p:
        return "guideline", basis
    return "policy", basis

def _disclosure_gap_summary(
    missing_api_in_guide: List[str],
    missing_api_in_policy: List[str],
    missing_supp_in_policy: List[str],
    declared_guide_not_observed: List[str],
    declared_policy_not_observed: List[str],
) -> dict:
    under_disclosure_paths = set(missing_api_in_guide) | set(missing_api_in_policy) | set(missing_supp_in_policy)
    over_disclosure_paths = set(declared_guide_not_observed) | set(declared_policy_not_observed)
    under_count = len(under_disclosure_paths)
    over_count = len(over_disclosure_paths)
    if under_count == 0 and over_count == 0:
        dominant = "aligned"
    elif under_count > over_count:
        dominant = "under_disclosure_more"
    elif over_count > under_count:
        dominant = "over_disclosure_more"
    else:
        dominant = "balanced"
    return {
        "dominant": dominant,
        "under_disclosure_nodes": under_count,
        "over_disclosure_nodes": over_count,
        "under_disclosure_detail": {
            "code_api_missing_in_guideline": len(missing_api_in_guide),
            "code_api_missing_in_policy": len(missing_api_in_policy),
            "code_supplement_missing_in_policy": len(missing_supp_in_policy),
        },
        "over_disclosure_detail": {
            "declared_guideline_not_observed": len(declared_guide_not_observed),
            "declared_policy_not_observed": len(declared_policy_not_observed),
        },
    }

def _guideline_governed(source_map: Dict[str, dict], path: str) -> bool:
    return ((source_map.get(path) or {}).get("match") or {}).get("scope") == "guideline_governed"

def _items_from_map(source_map: Dict[str, dict], term_key: str) -> List[dict]:
    return _items_from_paths(sorted(source_map), source_map, term_key)

def _items_from_paths(paths: Iterable[str], source_map: Dict[str, dict], term_key: str) -> List[dict]:
    items = []
    for p in sorted(paths):
        info = source_map.get(p, {}) or {}
        match = info.get("match") or {}
        item = {
            "hierarchy_path": p,
            "node_id": match.get("node_id", ""),
            "api_bound": bool(match.get("api_bound", False)),
            "scope": match.get("scope", ""),
            term_key: info.get("terms", []),
        }
        for key in ("flow_count", "detected_by", "source_apis", "source_files", "recipients"):
            if key in info:
                item[key] = info[key]
        items.append(item)
    return items

def _build_global_summary(platform: str, app_results: List[dict]) -> dict:
    closer_counts = {"guideline": 0, "policy": 0, "tie": 0}
    gap_counts = {"aligned": 0, "under_disclosure_more": 0, "over_disclosure_more": 0, "balanced": 0}
    for r in app_results:
        closer = (r.get("closer_to") or {}).get("api_scope", "tie")
        closer_counts[closer] = closer_counts.get(closer, 0) + 1
        gap = (r.get("disclosure_gap") or {}).get("dominant", "balanced")
        gap_counts[gap] = gap_counts.get(gap, 0) + 1

    def avg(key: str) -> Optional[float]:
        vals = [r["coverage"][key] for r in app_results if r["coverage"].get(key) is not None]
        return round(mean(vals), 4) if vals else None

    totals = {
        "code_nodes": 0,
        "code_api_nodes": 0,
        "code_supplement_nodes": 0,
        "code_unknown_flows": 0,
        "code_api_missing_in_guideline": 0,
        "code_api_missing_in_policy": 0,
        "code_supplement_missing_in_policy": 0,
        "declared_guideline_not_observed": 0,
        "declared_policy_not_observed": 0,
    }
    apps_with = {k: 0 for k in totals if k not in {"code_nodes", "code_api_nodes", "code_supplement_nodes", "code_unknown_flows"}}
    apps_with["code_unknown_flows"] = 0
    for r in app_results:
        stats = r["statistics"]
        for key in totals:
            totals[key] += int(stats.get(key, 0) or 0)
        for key in apps_with:
            if int(stats.get(key, 0) or 0) > 0:
                apps_with[key] += 1

    return {
        "platform": platform,
        "total_apps": len(app_results),
        "closer_to_distribution_api_scope": closer_counts,
        "disclosure_gap_distribution": gap_counts,
        "average_coverage": {
            "code_api_by_guideline": avg("code_api_by_guideline"),
            "code_api_by_policy": avg("code_api_by_policy"),
            "code_all_by_guideline": avg("code_all_by_guideline"),
            "code_all_by_policy": avg("code_all_by_policy"),
            "weighted_by_guideline": avg("weighted_by_guideline"),
            "weighted_by_policy": avg("weighted_by_policy"),
            "semantic_by_guideline": avg("semantic_by_guideline"),
            "semantic_by_policy": avg("semantic_by_policy"),
            "code_supplement_by_policy": avg("code_supplement_by_policy"),
        },
        "totals": totals,
        "apps_with_finding": apps_with,
        "metric_note": {
            "api_scope": f"Compares guideline and policy by semantic-profile coverage over code-relevant ontology categories with tie threshold={DEFAULT_CLOSER_TIE_THRESHOLD}.",
            "full_scope": "Policy/guideline coverage uses classifiable code nodes, including official APIs and user-input/supplement nodes.",
        },
    }

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
