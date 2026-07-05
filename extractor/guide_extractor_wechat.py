import re
import time
import coloredlogs, logging
from typing import List, Optional
from data.data_process.data_structures import PrivacyItem, ExtractionResult
from utils.text_utils import extract_sentences

class GuideExtractorWechat:
    def __init__(self, config, kb):
        self.config = config
        self.kb = kb
        self.logger = logging.getLogger(__name__)

        self.default_recipient = getattr(config, "guide_default_recipient", "开发者")

        self.processed_documents = 0
        self.pattern_a = re.compile(
            r"为了([^\n，。]*?)(?:[,，。]|，|。).*?(?:开发者|我们).*?"
            r"(收集|使用|访问|调用|读取).*?"
            r"你(?:的)?((?:选中的|选择的|挑选的|拍摄的|上传的|填写的)?[^，。；\n]+)",
            re.MULTILINE
        )
        self.pattern_b = re.compile(
            r"(?:开发者|我们).*?"
            r"(收集|使用|访问|调用|读取).*?"
            r"你(?:的)?((?:选中的|选择的|挑选的|拍摄的|上传的|填写的)?[^，。；\n]+?)"
            r"(?:[,，]\s*)?"
            r"(?:用于|以便于|为了)([^，。；\n]+)",
            re.MULTILINE
        )
    def extract_single_guide(self, text: str, app_id: Optional[str] = None) -> Optional[ExtractionResult]:
        start_time = time.time()
        start_m = re.search(r'开发者[：:]\s*', text)
        if not start_m:
            self.logger.warning(f"[{app_id}] 未找到“开发者”锚点，改为全文句分。")
            start_idx = 0
        else:
            start_idx = start_m.end()
        end_m = re.search(r'\r?\n?\s*用户权益(?:[：:]|\s*:)?', text[start_idx:])
        end_idx = start_idx + end_m.start() if end_m else len(text)
        core_text = text[start_idx:end_idx].strip()
        sentences = extract_sentences(core_text)
        self.logger.info(f"[{app_id}] 从指引中提取 {len(sentences)} 个句子用于规则抽取")

        privacy_items: List[PrivacyItem] = []

        matches_a = list(self.pattern_a.finditer(core_text))
        for i, match in enumerate(matches_a):
            purpose, method, data_type = match.group(1), match.group(2), match.group(3)
            item = PrivacyItem(
                data_type=self._normalize_wechat_data_type(data_type),
                purpose=self.kb.normalize_term((purpose or "").strip()),
                processing_method=(method or "").strip(),
                recipients=[self.default_recipient],
                source="guide_template_A",
                evidence_text=match.group(0).strip(),
                sentence_id=i
            )
            privacy_items.append(item)

        offset = len(privacy_items)
        matches_b = list(self.pattern_b.finditer(core_text))
        for j, match in enumerate(matches_b):
            method, data_type, purpose = match.group(1), match.group(2), match.group(3)
            item = PrivacyItem(
                data_type=self._normalize_wechat_data_type(data_type),
                purpose=self.kb.normalize_term((purpose or "").strip()),
                processing_method=(method or "").strip(),
                recipients=[self.default_recipient],
                source="guide_template_B",
                evidence_text=match.group(0).strip(),
                sentence_id=offset + j
            )
            privacy_items.append(item)
        extraction_time = time.time() - start_time
        privacy_items = self._filter_empty_data_type(privacy_items)
        result = ExtractionResult(
            app_id=app_id or "",
            privacy_items=privacy_items,
            total_sentences=len(privacy_items),
            processed_sentences=len(privacy_items),
            extraction_time=extraction_time,
            model_calls=0
        )
        self.logger.info(
            f"[{app_id}] 模板化指引抽取完成: {len(privacy_items)} 个隐私项，耗时 {extraction_time:.2f}s"
        )
        return result

    def _filter_empty_data_type(self, items: List[PrivacyItem]) -> List[PrivacyItem]:
        return [item for item in items if item.data_type and item.data_type.strip()]

    def _normalize_wechat_data_type(self, data_type: str) -> str:
        dt = self.kb.normalize_term((data_type or "").strip())
        dt = re.sub(r"\s+", "", dt)
        dt = dt.removeprefix("你的")
        if dt in {"", "的"}:
            return ""

        replacements = {
            "相册": "相册（仅写入）权限",
            "相册（仅写入）": "相册（仅写入）权限",
            "相册仅写入": "相册（仅写入）权限",
            "日历": "日历（仅写入）权限",
            "日历（仅写入）": "日历（仅写入）权限",
            "通讯录": "通讯录（仅写入）权限",
            "通讯录（仅写入）": "通讯录（仅写入）权限",
            "选中的照片或视频信息": "选中的照片或视频",
            "选择的照片或视频信息": "选中的照片或视频",
            "照片或视频信息": "选中的照片或视频",
            "文件": "选中的文件",
        }
        return replacements.get(dt, dt)
