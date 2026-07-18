import os
import re
from typing import Optional

class Config:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    source = "wechat"
    dataset = ""

    llm_backend = "claude"
    llm_model_name_deepseek = "deepseek-v4-pro"
    llm_api_deepseek = "replace-with-your-api-base-url"
    llm_api_key_deepseek = "replace-with-your-api-key"
    llm_model_name_qwen = "qwen3.6-plus"
    llm_api_qwen = "replace-with-your-api-base-url"
    llm_api_key_qwen = "replace-with-your-api-key"
    llm_model_name_gpt = "gpt-5.5"
    llm_api_gpt = "replace-with-your-api-base-url"
    llm_api_key_gpt = "replace-with-your-api-key"
    llm_model_name_claude = "claude-sonnet-4-6"
    llm_api_claude = "replace-with-your-api-base-url"
    llm_api_key_claude = "replace-with-your-api-key"
    llm_model_name_gemini = "gemini-3.1-pro-preview"
    llm_api_gemini = "replace-with-your-api-base-url"
    llm_api_key_gemini = "replace-with-your-api-key"

    llm_timeout = 180
    llm_max_retries = 3
    llm_retry_backoff_base = 2.0
    llm_retry_backoff_max = 60.0
    llm_retry_jitter = 0.25
    llm_max_concurrent_requests = 3
    llm_parallel_workers = 1
    llm_batch_size = 12
    llm_max_token = 4000
    llm_temperature = 0.0
    llm_top_p = 0.3

    app_parallel_workers = 6
    policy_use_llm = True

    DATA_DIR = os.path.join(PROJECT_ROOT, "data")
    POLICY_DIR = os.path.join(DATA_DIR, "policy", "wechat_policy")
    GUIDE_DIR = os.path.join(DATA_DIR, "guideline", "wechat_guideline")
    RESULTS_ROOT = os.path.join(PROJECT_ROOT, "results", "wechat")
    OUTPUT_DIR = RESULTS_ROOT

    ontology_base_file = "knowledge_ontology_wechat.json"
    use_ontology_normalization = True

    def __init__(self):
        self.source = os.getenv("SOURCE", self.source).strip().lower()
        if self.source not in {"wechat", "douyin", "alipay", "large"}:
            raise ValueError(f"Unsupported source: {self.source}")

        self.dataset = (
            os.getenv("DATASET")
            or os.getenv("PRIVGAP_DATASET")
            or self.dataset
        ).strip().lower()
        if self.dataset and self.dataset != "large":
            raise ValueError(f"Unsupported dataset: {self.dataset}")

        self.llm_backend = os.getenv("LLM_BACKEND", self.llm_backend).strip().lower()
        if self.llm_backend not in {"deepseek", "qwen", "gpt", "claude", "gemini"}:
            raise ValueError(f"Unsupported LLM backend: {self.llm_backend}")

        self.llm_timeout = self._env_int("LLM_TIMEOUT", self.llm_timeout, min_value=1)
        self.llm_max_retries = self._env_int("LLM_MAX_RETRIES", self.llm_max_retries, min_value=1)
        self.llm_retry_backoff_base = self._env_float(
            "LLM_RETRY_BACKOFF_BASE",
            self.llm_retry_backoff_base,
            min_value=0.0,
        )
        self.llm_retry_backoff_max = self._env_float(
            "LLM_RETRY_BACKOFF_MAX",
            self.llm_retry_backoff_max,
            min_value=0.0,
        )
        self.llm_retry_jitter = self._env_float(
            "LLM_RETRY_JITTER",
            self.llm_retry_jitter,
            min_value=0.0,
        )
        self.llm_max_concurrent_requests = self._env_int(
            "LLM_MAX_CONCURRENT_REQUESTS",
            self.llm_max_concurrent_requests,
            min_value=1,
        )
        self.llm_parallel_workers = self._env_int(
            "LLM_PARALLEL_WORKERS",
            self.llm_parallel_workers,
            min_value=1,
        )
        self.llm_batch_size = self._env_int("LLM_BATCH_SIZE", self.llm_batch_size, min_value=1)
        self.app_parallel_workers = self._env_int(
            "APP_PARALLEL_WORKERS",
            self.app_parallel_workers,
            min_value=1,
        )

        self._apply_llm_env()
        self._apply_source_paths(self.source)
        if self.dataset:
            self._apply_dataset_paths(self.dataset)

    def _apply_llm_env(self) -> None:
        for name in ("deepseek", "qwen", "gpt", "claude", "gemini"):
            upper = name.upper()
            model_attr = f"llm_model_name_{name}"
            api_attr = f"llm_api_{name}"
            key_attr = f"llm_api_key_{name}"
            setattr(self, model_attr, os.getenv(f"LLM_MODEL_NAME_{upper}", getattr(self, model_attr)))
            setattr(self, api_attr, os.getenv(f"LLM_API_{upper}", getattr(self, api_attr)))
            setattr(self, key_attr, os.getenv(f"LLM_API_KEY_{upper}", getattr(self, key_attr)))

        generic_model = os.getenv("LLM_MODEL_NAME")
        generic_api = os.getenv("LLM_API")
        generic_key = os.getenv("LLM_API_KEY") or os.getenv("PRIVGAP_API_KEY")
        if generic_model:
            setattr(self, f"llm_model_name_{self.llm_backend}", generic_model)
        if generic_api:
            setattr(self, f"llm_api_{self.llm_backend}", generic_api)
        if generic_key:
            setattr(self, f"llm_api_key_{self.llm_backend}", generic_key)

    def _apply_source_paths(self, source: str) -> None:
        self.platform_source = "wechat" if source == "large" else source
        if source == "large":
            self.POLICY_DIR = os.path.join(self.DATA_DIR, "policy", "large_policy")
            self.GUIDE_DIR = os.path.join(self.DATA_DIR, "guideline", "large_guideline")
            self.ontology_base_file = "knowledge_ontology_wechat.json"
        else:
            self.POLICY_DIR = os.path.join(self.DATA_DIR, "policy", f"{source}_policy")
            self.GUIDE_DIR = os.path.join(self.DATA_DIR, "guideline", f"{source}_guideline")
            self.ontology_base_file = f"knowledge_ontology_{source}.json"

        self.RESULTS_ROOT = os.path.join(self.PROJECT_ROOT, "results", source)
        self._set_output_dirs(self.RESULTS_ROOT)

    def _apply_dataset_paths(self, dataset: str) -> None:
        dataset_root = os.path.join(self.DATA_DIR, dataset)
        guideline_text_dir = os.path.join(dataset_root, "guideline", "text")
        self.POLICY_DIR = os.path.join(dataset_root, "policy")
        self.GUIDE_DIR = (
            guideline_text_dir
            if os.path.isdir(guideline_text_dir)
            else os.path.join(dataset_root, "guideline")
        )
        self.RESULTS_ROOT = os.path.join(self.PROJECT_ROOT, "results", dataset)
        self._set_output_dirs(self.RESULTS_ROOT)

    def _set_output_dirs(self, output_dir: str) -> None:
        self.OUTPUT_DIR = output_dir

    @staticmethod
    def safe_path_part(value: str) -> str:
        value = str(value or "").strip()
        value = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "unknown"

    @staticmethod
    def _env_int(name: str, default: int, min_value: Optional[int] = None) -> int:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got: {raw}") from exc
        if min_value is not None and value < min_value:
            raise ValueError(f"{name} must be at least {min_value}, got: {raw}")
        return value

    @staticmethod
    def _env_float(name: str, default: float, min_value: Optional[float] = None) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number, got: {raw}") from exc
        if min_value is not None and value < min_value:
            raise ValueError(f"{name} must be at least {min_value}, got: {raw}")
        return value

    def get_llm_model_name(self) -> str:
        return str(getattr(self, f"llm_model_name_{self.llm_backend}", "")).strip()

    def get_stage_results_root(self, stage: str) -> str:
        return os.path.join(self.RESULTS_ROOT, self.safe_path_part(stage))

    def get_model_results_root(self, stage: str) -> str:
        return os.path.join(
            self.get_stage_results_root(stage),
            self.safe_path_part(self.get_llm_model_name()),
        )

    def get_run_output_dir(self, stage: str, run_name: str) -> str:
        return os.path.join(
            self.get_model_results_root(stage),
            self.safe_path_part(run_name),
        )

    def get_stage_run_output_dir(self, stage: str, run_name: str) -> str:
        return os.path.join(
            self.get_stage_results_root(stage),
            self.safe_path_part(run_name),
        )
