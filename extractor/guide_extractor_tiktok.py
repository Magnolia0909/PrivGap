import re
import time
import coloredlogs, logging
from typing import List, Optional
from data.data_process.data_structures import PrivacyItem, ExtractionResult
from utils.text_utils import extract_sentences


class GuideExtractorDouyin:
    def __init__(self, config, kb):
        self.config = config
        self.kb = kb
        self.logger = logging.getLogger(__name__)

        self.default_recipient = "开发者"

        self.processed_documents = 0
        self.pattern = re.compile(
            r"开发者.*?收集.*?你(?:选中的|选择的|挑选的|拍摄的|上传的|填写的|的)?"
            r"([^，。；\n（(]+).*?用于([^，。；\n]+)",
            re.MULTILINE
        )
        self.register_login_pattern = re.compile(
            r"注册、登录：您可以通过手机号、身份证等创建账号，并完善相关的"
            r"网络识别信息（头像、昵称、密码等），收集这些信息是为了帮助您完成注册；"
            r"您可以使用抖音账号登录并使用本产品，经过您的同意，我们将获取您的"
            r"抖音账号的公开信息（头像、昵称以及您授权的其他信息）",
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

        sentence_id = 0
        for match in self.register_login_pattern.finditer(core_text):
            register_items = [
                ("手机号", "帮助您完成注册"),
                ("身份证", "帮助您完成注册"),
                ("网络识别信息（头像、昵称、密码等）", "帮助您完成注册"),
                ("公开信息（头像、昵称以及您授权的其他信息）", "使用抖音账号登录并使用本产品"),
            ]
            for data_type, purpose in register_items:
                privacy_items.append(PrivacyItem(
                    data_type=self.kb.normalize_term(data_type),
                    purpose=self.kb.normalize_term(purpose),
                    processing_method="收集",
                    recipients=["我们"],
                    source="guide_template_douyin_register_login",
                    evidence_text=match.group(0).strip(),
                    sentence_id=sentence_id
                ))
                sentence_id += 1

        matches = list(self.pattern.finditer(core_text))
        for match in matches:
            data_type, purpose = match.group(1), match.group(2)
            data_types = self._split_data_types(data_type)
            for dt in data_types:
                item = PrivacyItem(
                    data_type=self.kb.normalize_term(dt),
                    purpose=self.kb.normalize_term((purpose or "").strip()),
                    processing_method="收集",
                    recipients=[self.default_recipient],
                    source="guide_template_douyin",
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

    def _split_data_types(self, data_type: str) -> List[str]:
        raw = (data_type or "").strip()
        if not raw:
            return []
        return [part.strip() for part in re.split(r"[、/]", raw) if part.strip()]
