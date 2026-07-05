import random
import re
import threading
import time
import logging
import requests
from urllib3.util.retry import Retry
from urllib.parse import urlparse
from typing import Optional


class LLMContentFilterError(RuntimeError):
    pass


class LLMEmptyResponseError(RuntimeError):
    pass


class LLMWrapper:
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.backend = self.config.llm_backend
        self.temperature = self.config.llm_temperature
        self.top_p = self.config.llm_top_p
        self.max_tokens = self.config.llm_max_token
        self.timeout = self.config.llm_timeout
        self.max_retries = self.config.llm_max_retries
        self.retry_backoff_base = getattr(self.config, "llm_retry_backoff_base", 2.0)
        self.retry_backoff_max = getattr(self.config, "llm_retry_backoff_max", 60.0)
        self.retry_jitter = getattr(self.config, "llm_retry_jitter", 0.25)
        self.max_concurrent_requests = max(
            1,
            int(getattr(self.config, "llm_max_concurrent_requests", 1)),
        )
        self._request_semaphore = threading.BoundedSemaphore(self.max_concurrent_requests)

        if self.backend == "deepseek":
            self.model_name = self.config.llm_model_name_deepseek
            self.api_key = self.config.llm_api_key_deepseek
            self.api_url = self.config.llm_api_deepseek

        elif self.backend == "qwen":
            self.model_name = self.config.llm_model_name_qwen
            self.api_key = self.config.llm_api_key_qwen
            self.api_url = self.config.llm_api_qwen

        elif self.backend == "gpt":
            self.model_name = self.config.llm_model_name_gpt
            self.api_key = self.config.llm_api_key_gpt
            self.api_url = self.config.llm_api_gpt

        elif self.backend == "claude":
            self.model_name = self.config.llm_model_name_claude
            self.api_key = self.config.llm_api_key_claude
            self.api_url = self.config.llm_api_claude

        elif self.backend == "gemini":
            self.model_name = self.config.llm_model_name_gemini
            self.api_key = self.config.llm_api_key_gemini
            self.api_url = self.config.llm_api_gemini

        else:
            raise ValueError(f"[LLMWrapper] 不支持的 LLM 后端: {self.backend}")

        if not self.model_name:
            raise ValueError("[LLMWrapper] 配置错误：llm_model_name 不能为空。")
        if self.backend in ("deepseek", "qwen", "openai", "gpt", "claude", "gemini") and not self.api_key:
            raise ValueError(f"[LLMWrapper] 使用 {self.backend} 时必须提供 llm_api_key。")

    def chat(self, prompt: str, app_id: Optional[str] = None, task_type: str = "default") -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise TypeError("[LLMWrapper] prompt 必须是非空字符串。")

        for attempt in range(1, self.max_retries + 1):
            try:
                start_time = time.time()
                with self._request_semaphore:
                    response_text = self._send_request(prompt)
                elapsed = time.time() - start_time
                self.logger.info(
                    f"[{app_id}] LLMWrapper 调用成功 [{task_type}]: 模型={self.model_name}, 耗时={elapsed:.2f}s"
                )
                return response_text.strip()
            except Exception as e:
                self.logger.error(f"[{app_id}] 第 {attempt} 次模型调用失败: {e}")
                if isinstance(e, LLMContentFilterError):
                    raise RuntimeError(f"[LLMWrapper] 模型调用被内容审核拒绝: {e}")
                if attempt == self.max_retries:
                    raise RuntimeError(f"[LLMWrapper] 模型调用失败（已重试 {self.max_retries} 次）: {e}")
                sleep_seconds = self._retry_sleep_seconds(attempt)
                self.logger.warning(
                    f"[{app_id}] {sleep_seconds:.1f}s 后重试模型调用 "
                    f"({attempt + 1}/{self.max_retries})"
                )
                time.sleep(sleep_seconds)

    def _send_request(self, prompt: str) -> str:
        if self.backend in ("deepseek", "openai", "gpt", "claude", "gemini"):
            data = self._send_openai_compatible_request(prompt, connect_timeout=30)
            if "choices" not in data or not data["choices"]:
                raise ValueError(f"响应格式错误: {data}")
            content = data["choices"][0]["message"]["content"]
            if self.backend == "deepseek" and "<think>" in content:
                think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL)
                stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                stripped = re.sub(r"^```json\s*", "", stripped, flags=re.MULTILINE)
                stripped = re.sub(r"^```\s*", "", stripped, flags=re.MULTILINE).strip()
                if stripped:
                    content = stripped
                elif think_match:
                    inner = think_match.group(1).strip()
                    json_match = re.search(r"\{.*\}", inner, flags=re.DOTALL)
                    content = json_match.group(0) if json_match else inner

            return content

        elif self.backend == "qwen":
            data = self._send_openai_compatible_request(prompt, connect_timeout=10)
            return self._extract_openai_response_content(data)

        else:
            raise NotImplementedError(f"尚未支持的后端: {self.backend}")

    def _extract_openai_response_content(self, data: dict) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"OpenAI 兼容响应格式异常: {data}")

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError(f"OpenAI 兼容响应缺少 message: {data}")

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return self._strip_thinking_content(content)

        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str):
            json_payload = self._extract_json_object(reasoning)
            if json_payload:
                return json_payload

        finish_reason = choice.get("finish_reason") or "unknown"
        raise LLMEmptyResponseError(
            "上游返回 HTTP 成功，但 message.content 为空"
            f"（finish_reason={finish_reason}）"
        )

    def _strip_thinking_content(self, content: str) -> str:
        think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
        stripped = re.sub(r"^```json\s*", "", stripped, flags=re.MULTILINE | re.IGNORECASE)
        stripped = re.sub(r"^```\s*", "", stripped, flags=re.MULTILINE).strip()
        if stripped:
            return stripped
        if think_match:
            json_payload = self._extract_json_object(think_match.group(1))
            if json_payload:
                return json_payload
        raise LLMEmptyResponseError("上游仅返回无最终答案的 think 块")

    def _extract_json_object(self, text: str) -> Optional[str]:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        return match.group(0).strip() if match else None

    def _send_openai_compatible_request(self, prompt: str, connect_timeout: int) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Connection": "close",
        }
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["POST"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        session = requests.Session()
        if self._should_bypass_env_proxy():
            session.trust_env = False
        adapter = requests.adapters.HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        try:
            resp = session.post(
                self.api_url,
                headers=headers,
                json=payload,
                timeout=(connect_timeout, self.timeout),
            )
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                detail = self._response_error_detail(resp)
                if resp.status_code == 400 and self._is_content_filter_detail(detail):
                    raise LLMContentFilterError(detail) from e
                raise requests.HTTPError(f"{e}; response={detail}", response=resp) from e
            return resp.json()
        finally:
            session.close()

    def _retry_sleep_seconds(self, attempt: int) -> float:
        base = max(0.0, float(self.retry_backoff_base))
        delay = base * (2 ** max(0, attempt - 1))
        max_delay = max(0.0, float(self.retry_backoff_max))
        if max_delay:
            delay = min(delay, max_delay)
        jitter = max(0.0, float(self.retry_jitter))
        if jitter:
            delay += random.uniform(0, jitter * max(1.0, delay))
        return delay

    @staticmethod
    def _response_error_detail(resp: requests.Response) -> str:
        text = (resp.text or "").strip().replace("\n", " ")
        if len(text) > 500:
            text = text[:500] + "..."
        return text or "<empty>"

    @staticmethod
    def _is_content_filter_detail(detail: str) -> bool:
        lowered = (detail or "").lower()
        markers = (
            "prohibited content",
            "content policy",
            "content_filter",
            "safety",
            "内容审核",
            "敏感内容",
        )
        return any(marker in lowered for marker in markers)

    def _should_bypass_env_proxy(self) -> bool:
        host = urlparse(self.api_url).hostname or ""
        return host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com")
