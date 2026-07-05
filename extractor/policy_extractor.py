import os
import time
import coloredlogs, logging
from typing import List, Optional

from data.data_process.data_structures import PrivacyItem, ExtractionResult
from utils.text_utils import extract_sentences

from extractor.llm_extractor import LLMExtractor


class PolicyExtractor:
    def __init__(self, config, kb, llm_client=None):
        self.config = config
        self.kb = kb
        self.logger = logging.getLogger(__name__)
        self.use_llm = self.config.policy_use_llm

        self.llm_extractor = (
            LLMExtractor(config, kb, llm_client)
            if self.use_llm and llm_client
            else None
        )

    def extract_single_policy(self, text: str, app_id: Optional[str] = None) -> ExtractionResult:
        if not text or not isinstance(text, str):
            raise TypeError(f"[PolicyExtractor]输入文本无效: {type(text)}")
        start_time = time.time()
        sentences = extract_sentences(text)
        if not sentences:
            raise ValueError(f"[{app_id}]无法从文本中提取有效句子。")
        privacy_items: List[PrivacyItem] = []
        model_calls = 0

        if self.llm_extractor:
            max_attempts = 3
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    llm_items, llm_calls = self.llm_extractor.extract(sentences, app_id)
                    privacy_items.extend(llm_items)
                    model_calls += llm_calls
                    self.logger.info(f"[{app_id}] LLMExtractor抽取了{len(llm_items)}条。")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    self.logger.error(
                        f"[{app_id}] LLMExtractor抽取失败(第{attempt}次), 已重试: {e}"
                    )
            if last_err is not None:
                self.logger.error(f"[{app_id}] LLMExtractor抽取失败, 已跳过LLM阶段:{last_err}")
                self._record_llm_failure(app_id, str(last_err))

        elapsed = time.time() - start_time
        normalized_items = [self._normalize_item(item) for item in privacy_items]
        deduped_items = self._dedup_items(normalized_items)

        result = ExtractionResult(
            app_id=app_id or "",
            privacy_items=deduped_items,
            total_sentences=len(sentences),
            processed_sentences=len(deduped_items),
            extraction_time=elapsed,
            model_calls=model_calls
        )
        self.logger.info(
            f"[{app_id}] Policy 抽取完成：共 {len(deduped_items)} 条隐私项,"
            f"句子数 {len(sentences)}, 耗时 {elapsed:.2f}s, LLM 调用 {model_calls} 次。"
        )
        return result

    def _normalize_item(self, item: PrivacyItem) -> PrivacyItem:
        item.data_type = item.data_type.strip() if item.data_type else ""
        item.purpose = self.kb.normalize_term(item.purpose.strip()) if item.purpose else ""
        if item.recipients:
            uniq = []
            for r in item.recipients:
                if r and r not in uniq:
                    uniq.append(r)
            item.recipients = uniq
        return item