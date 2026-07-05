import re
import time
import coloredlogs, logging
from typing import List, Optional
from data.data_process.data_structures import PrivacyItem, ExtractionResult
from utils.text_utils import extract_sentences


class GuideExtractorAlipay:
    def __init__(self, config, kb):
        self.config = config
        self.kb = kb
        self.logger = logging.getLogger(__name__)

        self.default_recipient = "我们"

        self.processed_documents = 0
        self.pattern = re.compile(
            r"收集[你您]的(.+?)信息\s*[,，]\s*用于为[你您]([^。；;\n]+)",
            re.MULTILINE
        )

    def extract_single_guide(self, text: str, app_id: Optional[str] = None) -> Optional[ExtractionResult]:
        start_time = time.time()
        core_text = text.strip()
        sentences = extract_sentences(core_text)
        self.logger.info(f"[{app_id}] 从指引中提取 {len(sentences)} 个句子用于规则抽取")

        privacy_items: List[PrivacyItem] = []

        matches = list(self.pattern.finditer(core_text))
        sentence_id = 0
        for _, match in enumerate(matches):
            data_type_raw, purpose = match.group(1), match.group(2)
            if data_type_raw is None:
                continue
            data_types = [s.strip() for s in data_type_raw.split("、") if s.strip()]
            for dt in data_types:
                item = PrivacyItem(
                    data_type=self.kb.normalize_term(dt),
                    purpose=self.kb.normalize_term((purpose or "").strip()),
                    processing_method="收集",
                    recipients=[self.default_recipient],
                    source="guide_template_alipay",
                    evidence_text=match.group(0).strip(),
                    sentence_id=sentence_id
                )
                privacy_items.append(item)
                sentence_id += 1

        extraction_time = time.time() - start_time
        result = ExtractionResult(
            app_id=app_id or "",
            privacy_items=privacy_items,
            total_sentences=len(privacy_items),
            processed_sentences=len(privacy_items),
            extraction_time=extraction_time,
            model_calls=0
        )
        privacy_items = self._filter_empty_data_type(privacy_items)
        result.privacy_items = privacy_items
        self.logger.info(
            f"[{app_id}] 模板化指引抽取完成: {len(privacy_items)} 个隐私项，耗时 {extraction_time:.2f}s"
        )
        return result

    def _filter_empty_data_type(self, items: List[PrivacyItem]) -> List[PrivacyItem]:
        return [item for item in items if item.data_type and item.data_type.strip()]
