import json
import os

from data.data_process.data_structures import ExtractionResult


def get_extraction_output_dir(cfg, doc_type: str, doc_subdir: str | None = None) -> str:
    if doc_subdir:
        return os.path.join(cfg.OUTPUT_DIR, "extractions", doc_subdir)
    return os.path.join(cfg.OUTPUT_DIR, "extractions", doc_type)


def get_extraction_result_path(cfg, app_id: str, doc_type: str, doc_subdir: str | None = None) -> str:
    return os.path.join(
        get_extraction_output_dir(cfg, doc_type, doc_subdir),
        f"{app_id}_privacy_items.json",
    )


def save_extraction_result(cfg, result: ExtractionResult, doc_type: str, doc_subdir: str | None = None):
    out_dir = get_extraction_output_dir(cfg, doc_type, doc_subdir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{result.app_id}_privacy_items.json")
    items = [_serialize_item(item, doc_type) for item in result.privacy_items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _serialize_item(item, doc_type: str) -> dict:
    data = item.to_dict()
    if doc_type == "guide":
        data.pop("confidence", None)
    return data


def has_valid_extraction_result(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    return isinstance(data, list) and len(data) > 0
