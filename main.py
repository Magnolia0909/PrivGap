import os
import argparse
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from config.config import Config
from extractor.guide_extractor_wechat import GuideExtractorWechat
from extractor.guide_extractor_alipay import GuideExtractorAlipay
from extractor.guide_extractor_douyin import GuideExtractorDouyin
from extractor.policy_extractor import PolicyExtractor
from extractor.llm_wrapper import LLMWrapper
from ontology.ontology_base import OntologyBase
from ontology.guideline_policy_consistency import compare_extraction_dirs
from ontology.code_flow_observation_adapter import build_code_ontology_observations
from ontology.code_guideline_policy_consistency import compare_three_way
from utils.extraction_result_io import (
    get_extraction_result_path,
    has_valid_extraction_result,
    save_extraction_result,
)
from utils.run_config import build_guideline_config, build_ontology_config, build_policy_config
from utils.text_loader import load_texts_from_dir
from flow_analysis.config import default_pred_csv as default_flow_pred_csv
from flow_analysis.csv_io import write_csv as save_taint_chains_csv
from flow_analysis.pipeline import run_platform as run_flow_platform


def has_fresh_extraction_result(result_path: str, source_path: str) -> bool:
    if not has_valid_extraction_result(result_path):
        return False
    if not os.path.isfile(source_path):
        return True
    return os.path.getmtime(result_path) >= os.path.getmtime(source_path)


def _txt_app_ids(dir_path: str) -> set[str]:
    if not os.path.isdir(dir_path):
        return set()
    return {
        os.path.splitext(name)[0].strip()
        for name in os.listdir(dir_path)
        if name.endswith(".txt") and os.path.splitext(name)[0].strip()
    }


def _platform_from_cfg(cfg) -> str:
    source = getattr(cfg, "source", "")
    return getattr(cfg, "platform_source", None) or ("wechat" if source == "large" else source)


def _default_code_dir(cfg) -> str:
    env_dir = os.getenv("CODE_DIR") or os.getenv("APP_CODE_DIR") or os.getenv("PRIVGAP_APP_SOURCE")
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))
    if getattr(cfg, "dataset", "") == "large":
        return os.path.join(cfg.DATA_DIR, "large", "code", "decompile")
    return os.path.join(cfg.DATA_DIR, "code", _platform_from_cfg(cfg))


def _extract_code_app_id(dir_name: str, platform: str) -> str:
    name = dir_name.strip()
    if not name:
        return ""

    parts = [part.strip() for part in name.split("_") if part.strip()]
    if platform == "wechat":
        return name
    if platform == "douyin":
        if len(parts) > 1 and parts[-1].startswith("tt"):
            return "_".join(parts[:-1])
        return name
    if platform == "alipay":
        match = re.fullmatch(r"\d{8,}_(.+)", name)
        if match:
            return match.group(1).strip()
    return name


def _code_app_id_map(code_dir: str, platform: str) -> dict[str, str]:
    if not os.path.isdir(code_dir):
        raise FileNotFoundError(f"code_dir does not exist: {code_dir}")
    app_ids = {}
    for name in os.listdir(code_dir):
        path = os.path.join(code_dir, name)
        if not os.path.isdir(path):
            continue
        app_id = _extract_code_app_id(name, platform)
        if app_id:
            app_ids[app_id] = name
    return app_ids


def _copy_filtered_extractions(output_dir: str, app_ids: set[str]) -> str:
    src_root = os.path.join(output_dir, "extractions")
    dst_root = os.path.join(output_dir, "extractions_with_code")
    if os.path.isdir(dst_root):
        shutil.rmtree(dst_root)
    if not os.path.isdir(src_root):
        return dst_root

    suffix = "_privacy_items.json"
    for root, _dirs, files in os.walk(src_root):
        rel_root = os.path.relpath(root, src_root)
        for fname in files:
            if not fname.endswith(suffix):
                continue
            app_id = fname[:-len(suffix)]
            if app_id not in app_ids:
                continue
            src = os.path.join(root, fname)
            dst_dir = dst_root if rel_root == "." else os.path.join(dst_root, rel_root)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(dst_dir, fname))
    return dst_root


def validate_paired_source_app_ids(cfg, app_id: str | None = None) -> None:
    guide_ids = _txt_app_ids(cfg.GUIDE_DIR)
    policy_ids = _txt_app_ids(cfg.POLICY_DIR)
    if app_id:
        missing = []
        if app_id not in guide_ids:
            missing.append("guideline")
        if app_id not in policy_ids:
            missing.append("policy")
        if missing:
            raise ValueError(f"app_id={app_id} is missing paired source documents: {', '.join(missing)}")
        return

    only_guide = sorted(guide_ids - policy_ids)
    only_policy = sorted(policy_ids - guide_ids)
    if only_guide or only_policy:
        raise ValueError(
            "Paired source app set mismatch. "
            f"only_guideline={len(only_guide)}, only_policy={len(only_policy)}. "
            f"examples_only_guideline={only_guide[:10]}, examples_only_policy={only_policy[:10]}."
        )


def run_policy_extraction(cfg, app_id: str | None = None, app_ids: set[str] | None = None):
    policy_data = load_texts_from_dir(cfg.POLICY_DIR, app_id=app_id)
    guide_data = load_texts_from_dir(cfg.GUIDE_DIR, app_id=app_id)
    if app_ids is not None:
        policy_data = {aid: text for aid, text in policy_data.items() if aid in app_ids}
        guide_data = {aid: text for aid, text in guide_data.items() if aid in app_ids}

    policy_ids = set(policy_data.keys())
    guide_ids = set(guide_data.keys())
    common_ids = policy_ids & guide_ids

    strict_pairing = getattr(cfg, "dataset", "") == "large"
    missing_policy_ids = guide_ids - policy_ids
    if strict_pairing and missing_policy_ids:
        raise ValueError(f"large dataset has guidelines without matching policies: {sorted(missing_policy_ids)[:20]} (total={len(missing_policy_ids)})")
    if strict_pairing:
        policy_data = {aid: text for aid, text in policy_data.items() if aid in common_ids}

    if app_id and app_id not in common_ids:
        raise ValueError(f"app_id is missing or not paired: {app_id}")

    ontology_base = OntologyBase(cfg)
    llm = LLMWrapper(cfg)

    policy_extractor = PolicyExtractor(cfg, ontology_base, llm_client=llm)

    def process_app(task_app_id, text):
        result_path = get_extraction_result_path(cfg, task_app_id, "policy")
        source_path = os.path.join(cfg.POLICY_DIR, f"{task_app_id}.txt")
        if has_fresh_extraction_result(result_path, source_path):
            return
        result = policy_extractor.extract_single_policy(text, task_app_id)
        save_extraction_result(cfg, result, "policy")

    max_workers = getattr(cfg, "app_parallel_workers", 1)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_app, task_app_id, text)
            for task_app_id, text in policy_data.items()
        }

        for future in as_completed(futures):
            future.result()


def run_guideline_pipeline(run_tag: str, app_id: str | None = None, app_ids: set[str] | None = None):
    cfg = build_guideline_config(run_tag)

    guide_data = load_texts_from_dir(cfg.GUIDE_DIR, app_id=app_id)
    if app_ids is not None:
        guide_data = {aid: text for aid, text in guide_data.items() if aid in app_ids}
    if app_id and not guide_data:
        raise ValueError(f"guideline is missing or empty: {app_id}")

    ontology_base = OntologyBase(cfg)
    if cfg.source == "alipay":
        guide_extractor = GuideExtractorAlipay(cfg, ontology_base)
    elif cfg.source == "douyin":
        guide_extractor = GuideExtractorDouyin(cfg, ontology_base)
    else:
        guide_extractor = GuideExtractorWechat(cfg, ontology_base)

    def process_guide(task_app_id, text):
        result_path = get_extraction_result_path(cfg, task_app_id, "guide")
        source_path = os.path.join(cfg.GUIDE_DIR, f"{task_app_id}.txt")
        if has_fresh_extraction_result(result_path, source_path):
            return
        result = guide_extractor.extract_single_guide(text, task_app_id)
        save_extraction_result(cfg, result, "guide")

    max_workers = getattr(cfg, "app_parallel_workers", 1)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_guide, task_app_id, text)
            for task_app_id, text in guide_data.items()
        }

        for future in as_completed(futures):
            future.result()

    guide_extractions_dir = os.path.join(cfg.OUTPUT_DIR, "extractions")
    if app_ids is not None:
        guide_extractions_dir = _copy_filtered_extractions(cfg.OUTPUT_DIR, app_ids)

    return guide_extractions_dir


def run_flow_analysis_pipeline(
    platform: str,
    app_ids: set[str] | None = None,
):
    chains = run_flow_platform(platform, app_ids=app_ids)
    pred_csv = default_flow_pred_csv(platform)
    pred_csv.parent.mkdir(parents=True, exist_ok=True)
    save_taint_chains_csv(chains, pred_csv)
    return {"pred_csv": pred_csv}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", type=str, default=None)
    parser.add_argument(
        "--code-dir",
        type=str,
        default=None,
    )
    args = parser.parse_args()

    run_tag_prefix = os.getenv("RUN_TAG_PREFIX", "").strip()
    run_tag_suffix = os.getenv("RUN_TAG_SUFFIX", "").strip()
    disable_semantic_ontology_fallback = os.getenv("DISABLE_SEMANTIC_ONTOLOGY_FALLBACK", "").strip().lower() in {"1", "true", "yes", "y"}
    use_semantic_ontology_fallback = not disable_semantic_ontology_fallback
    consistency_app_id_scope = "intersection"
    guide_run_tag_base = os.getenv("GUIDE_RUN_TAG", "guide").strip()
    tag_cfg = Config()
    is_large_dataset = getattr(tag_cfg, "dataset", "") == "large" or getattr(tag_cfg, "source", "") == "large"
    if is_large_dataset:
        validate_paired_source_app_ids(tag_cfg, app_id=args.app_id)

    code_dir = os.path.abspath(os.path.expanduser(args.code_dir)) if args.code_dir else _default_code_dir(tag_cfg)
    platform_for_code = _platform_from_cfg(tag_cfg)
    code_id_map = _code_app_id_map(code_dir, platform_for_code)
    code_ids = set(code_id_map)
    paired_source_ids = _txt_app_ids(tag_cfg.GUIDE_DIR) & _txt_app_ids(tag_cfg.POLICY_DIR)
    code_app_ids = code_ids & paired_source_ids
    if args.app_id:
        code_app_ids = {args.app_id} & code_app_ids
    if not code_app_ids:
        raise ValueError("No mini-apps selected for this run.")
    flow_code_app_ids = {code_id_map[aid] for aid in code_app_ids if aid in code_id_map}

    def build_run_tag(base: str) -> str:
        parts = [
            tag_cfg.safe_path_part(run_tag_prefix) if run_tag_prefix else "",
            tag_cfg.safe_path_part(base),
            tag_cfg.safe_path_part(run_tag_suffix) if run_tag_suffix else "",
        ]
        return "_".join(part for part in parts if part)

    def build_guide_run_tag() -> str:
        return tag_cfg.safe_path_part(guide_run_tag_base or "guide")

    def ontology_consistency_dir(label: str) -> str:
        return os.path.join(
            tag_cfg.RESULTS_ROOT,
            "ontology_consistency",
            tag_cfg.safe_path_part(label),
        )

    model_tag = tag_cfg.safe_path_part(tag_cfg.get_llm_model_name())
    policy_run_tag = build_run_tag(model_tag)
    base_consistency_tag = build_run_tag(f"{model_tag}_base")
    ontology_consistency_tag = build_run_tag(f"{model_tag}_ontology")

    scoped_app_ids = code_app_ids or ({args.app_id} if args.app_id else None)
    flow_platform = _platform_from_cfg(tag_cfg)
    if flow_platform not in {"wechat", "alipay", "douyin"}:
        raise ValueError(f"flow-analysis only supports wechat/alipay/douyin, got: {flow_platform}")
    flow_run = run_flow_analysis_pipeline(
        flow_platform,
        app_ids=flow_code_app_ids,
    )
    code_observation_dir = os.path.join(tag_cfg.RESULTS_ROOT, "flow_analysis", "ontology_observations")
    physical_to_clean_app_id = {physical: clean for clean, physical in code_id_map.items()}
    build_code_ontology_observations(
        flow_platform,
        flow_run["pred_csv"],
        code_observation_dir,
        app_id=code_id_map.get(args.app_id) if args.app_id else None,
        app_ids=flow_code_app_ids,
        app_id_map=physical_to_clean_app_id,
    )

    guide_tag = build_guide_run_tag()
    guide_extractions_dir = run_guideline_pipeline(guide_tag, app_id=args.app_id, app_ids=code_app_ids)

    cfg = build_policy_config(policy_run_tag, False)
    run_policy_extraction(cfg, app_id=args.app_id, app_ids=code_app_ids)
    base_policy_extractions_dir = os.path.join(cfg.OUTPUT_DIR, "extractions")
    if code_app_ids is not None:
        base_policy_extractions_dir = _copy_filtered_extractions(cfg.OUTPUT_DIR, code_app_ids)

    ontology_cfg = build_ontology_config(ontology_consistency_tag)

    compare_extraction_dirs(
        platform=getattr(ontology_cfg, "platform_source", ontology_cfg.source),
        guide_dir=guide_extractions_dir,
        policy_dir=base_policy_extractions_dir,
        out_dir=ontology_consistency_dir(f"{base_consistency_tag}_guide_policy"),
        use_ontology_alignment=False,
        use_semantic_fallback=False,
        app_id_scope=consistency_app_id_scope,
        app_ids=scoped_app_ids,
    )

    compare_extraction_dirs(
        platform=getattr(ontology_cfg, "platform_source", ontology_cfg.source),
        guide_dir=guide_extractions_dir,
        policy_dir=base_policy_extractions_dir,
        out_dir=ontology_consistency_dir(f"{ontology_consistency_tag}_guide_policy"),
        use_ontology_alignment=True,
        use_semantic_fallback=use_semantic_ontology_fallback,
        app_id_scope=consistency_app_id_scope,
        app_ids=scoped_app_ids,
    )

    code_consistency_tag = build_run_tag(f"{model_tag}_code_guideline_policy_from_flow")
    code_consistency_out = ontology_consistency_dir(code_consistency_tag)
    compare_three_way(
        platform=getattr(ontology_cfg, "platform_source", ontology_cfg.source),
        code_dir=code_observation_dir,
        guide_dir=guide_extractions_dir,
        policy_dir=base_policy_extractions_dir,
        out_dir=code_consistency_out,
        use_semantic_fallback=use_semantic_ontology_fallback,
        app_id_filter=scoped_app_ids,
    )



if __name__ == "__main__":
    main()
