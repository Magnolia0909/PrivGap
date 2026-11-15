from __future__ import annotations
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from flow_analysis.config import api_dict_path

API_PREFIX = {"wechat": "wx.", "alipay": "my.", "douyin": "tt."}
                              
EXTRA_SOURCE_APIS: dict[str, set[str]] = {
    "wechat": {
        "wx.getStorage",
        "wx.getStorageSync",
        "wx.login",
        "wx.getFileInfo",
        "wx.getImageInfo",
        "wx.getVideoInfo",
        "wx.getSystemInfo",
        "wx.getSystemInfoSync",
        "wx.getDeviceInfo",
        "wx.getDeviceInfoSync",
        "wx.getWindowInfo",
        "wx.getAppBaseInfo",
        "wx.getNetworkType",
        "wx.getAccountInfoSync",
        "wx.getEnterOptionsSync",
        "wx.getLaunchOptionsSync",
    },
    "alipay": {
        "my.getStorage",
        "my.getStorageSync",
        "my.getAuthCode",
        "my.login",
        "my.getPhoneNumber",
        "my.getUserInfo",
        "my.getSystemInfo",
        "my.getSystemInfoSync",
        "my.getDeviceInfo",
        "my.getDeviceInfoSync",
        "my.getNetworkType",
        "my.getFileInfo",
        "my.getImageInfo",
        "my.scan",
        "my.scanCode",
        "my.getCurrentPosition",
    },
    "douyin": {
        "tt.getStorage",
        "tt.getStorageSync",
        "tt.login",
        "tt.getPhoneNumber",
        "tt.getUserInfo",
        "tt.getUserProfile",
        "tt.getSystemInfo",
        "tt.getSystemInfoSync",
        "tt.getDeviceInfo",
        "tt.getDeviceInfoSync",
        "tt.getNetworkType",
        "tt.getFileInfo",
        "tt.getImageInfo",
        "tt.getEnterOptionsSync",
        "tt.getLaunchOptionsSync",
        "tt.showDouyinOpenAuth",
    },
}
                                                                 
_CORE_SINK_FLOW_TYPE: dict[str, str] = {
    "request": "network_request",
    "httpRequest": "network_request",
    "downloadFile": "network_request",
    "uploadFile": "file_upload",
    "setStorage": "local_storage",
    "setStorageSync": "local_storage",
    "saveImageToPhotosAlbum": "album_write",
    "saveVideoToPhotosAlbum": "album_write",
}

_WRAPPER_SINK_FLOW_TYPE: dict[str, str] = {
    "ajax": "network_request",
    "http": "network_request",
    "post": "network_request",
    "$post": "network_request",
    "requestApi": "network_request",
    "sendRequest": "network_request",
    "upload": "file_upload",
    "upLoad": "file_upload",
    "uploadImage": "file_upload",
    "asyncUpload": "file_upload",
}

_RARE_SINK_FLOW_TYPE: dict[str, str] = {
    "setClipboardData": "clipboard",
    "setClipboard": "clipboard",
    "makePhoneCall": "phone_call",
    "requestPayment": "payment",
    "tradePay": "payment",
}
                                              
_SINK_FLOW_TYPE: dict[str, str] = {**_CORE_SINK_FLOW_TYPE, **_WRAPPER_SINK_FLOW_TYPE, **_RARE_SINK_FLOW_TYPE}
                                                                  
_COMMON_SINK_PAYLOAD_KEYS: dict[str, list[str]] = {
    "setStorageSync": ["$arg1"],
    "setStorage": ["data"],
    "request": ["url", "data", "header", "headers"],
    "httpRequest": ["url", "data", "header", "headers"],
    "downloadFile": ["url", "header", "headers"],
    "uploadFile": ["url", "filePath", "filepath", "fileName", "formData", "header", "headers"],
    "saveImageToPhotosAlbum": ["filePath"],
    "saveVideoToPhotosAlbum": ["filePath"],
}

_PLATFORM_SINK_PAYLOAD_KEYS: dict[str, dict[str, list[str]]] = {
    "alipay": {
        "ajax": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "http": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "post": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "get": ["$arg1", "url", "data", "params", "query", "header", "headers"],
        "$post": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "requestApi": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "sendRequest": ["$arg1", "url", "data", "params", "body", "header", "headers"],
        "upload": ["$arg1", "url", "filePath", "fileName", "formData", "header", "headers"],
        "upLoad": ["$arg1", "url", "filePath", "fileName", "formData", "header", "headers"],
        "uploadImage": ["$arg1", "url", "filePath", "fileName", "formData", "header", "headers"],
        "asyncUpload": ["$arg1", "url", "filePath", "fileName", "formData", "header", "headers"],
    },
}
                                                                                                                                        
_COMMON_SUPPRESSED_FLOW_PATTERNS: list[dict[str, str]] = [
    {"source": "login", "flow_type": "local_storage"},
    {"source": "chooseImage", "sink": "request"},
    {"source": "chooseMedia", "sink": "request"},
    {"source": "getStorageSync", "flow_type": "album_write"},
    {"source": "getUserProfile", "sink": "request"},
    {"source": "getSystemInfo", "flow_type": "local_storage"},
    {"source": "getSystemInfoSync", "flow_type": "local_storage"},
]

_PLATFORM_SUPPRESSED_FLOW_PATTERNS: dict[str, list[dict[str, str]]] = {}
                                                           
_PLATFORM_AUTO_UI_HANDLER_PATTERNS: dict[str, list[str]] = {}

_COMMON_SEMANTIC_SINK_PATTERNS: list[dict[str, str]] = [
    {"pattern": r"^uploadFile\d*$", "canonical_method": "uploadFile", "flow_type": "file_upload"},
    {"pattern": r"^(?:request|httpRequest)\d*$", "canonical_method": "request", "flow_type": "network_request"},
]

_PLATFORM_SEMANTIC_SINK_PATTERNS: dict[str, list[dict[str, str]]] = {
    "wechat": [],
    "alipay": [
        {
            "pattern": r"^(?:.*login.*|.*register.*|.*auth.*|usePhoneNumberLogin)$",
            "flow_type": "network_request",
            "source_methods": ["getAuthCode", "getPhoneNumber"],
            "structural_confirm": "when_resolved",
        },
    ],
    "douyin": [
        {"pattern": r"^upload(?:Img|Image)\d*$", "flow_type": "file_upload"},
        {"pattern": r"^\$?post$", "flow_type": "network_request"},
        {
            "pattern": r"^(?:.*login.*|.*auth.*|.*authorization.*|update.*user.*)$",
            "flow_type": "network_request",
            "source_methods": ["getPhoneNumber", "getUserProfile", "getUserInfo", "login"],
        },
        {
            "pattern": r"^getUploadParams$",
            "flow_type": "file_upload",
            "source_methods": ["chooseImage", "chooseMedia"],
        },
    ],
}

def sink_payload_key_map(platform: str | None = None) -> dict[str, list[str]]:
    payload = {k: list(v) for k, v in _COMMON_SINK_PAYLOAD_KEYS.items()}
    payload.update({k: list(v) for k, v in _PLATFORM_SINK_PAYLOAD_KEYS.get(platform or "", {}).items()})
    return payload

def core_sink_methods() -> list[str]:
    return sorted(_CORE_SINK_FLOW_TYPE.keys())

def suppressed_flow_patterns(platform: str | None = None) -> list[dict[str, str]]:
    return [*_COMMON_SUPPRESSED_FLOW_PATTERNS, *_PLATFORM_SUPPRESSED_FLOW_PATTERNS.get(platform or "", [])]

_PLATFORM_RESOURCE_SINK_PATTERNS: dict[str, list[dict[str, str]]] = {
    "wechat": [
        {
            "factory": "HttpResource",
            "method": "saveAsync",
            "endpoint_pattern": r"(?:quicklogin|mobile_login|phone|auth)",
            "flow_type": "network_request",
            "source_methods": ["getPhoneNumber"],
        },
    ],
}

def resource_sink_patterns(platform: str | None = None) -> list[dict[str, str]]:
    return [dict(item) for item in _PLATFORM_RESOURCE_SINK_PATTERNS.get(platform or "", [])]

def semantic_sink_patterns(platform: str | None = None) -> list[dict[str, str]]:
    return [dict(item) for item in [*_COMMON_SEMANTIC_SINK_PATTERNS, *_PLATFORM_SEMANTIC_SINK_PATTERNS.get(platform or "", [])]]

def auto_ui_handler_patterns(platform: str | None = None) -> list[str]:
    return list(_PLATFORM_AUTO_UI_HANDLER_PATTERNS.get(platform or "", []))
                                      
_PLATFORM_UI_EVENT_SOURCE_METHODS: dict[str, list[str]] = {
    "douyin": ["getPhoneNumber"],
}

def ui_event_source_methods(platform: str | None = None) -> list[str]:
    return list(_PLATFORM_UI_EVENT_SOURCE_METHODS.get(platform or "", []))

def storage_key_sensitive_local_storage(platform: str | None = None) -> bool:
    value = os.getenv("FLOW_ANALYSIS_STORAGE_KEY_SENSITIVE", "").strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    return platform in {"wechat", "alipay"}

def wrapper_terminal_mode(platform: str | None = None) -> str:
    value = os.getenv("FLOW_ANALYSIS_WRAPPER_TERMINAL_MODE", "").strip().lower()
    if value in {"official", "entry", "outer", "entry+official", "outer+official", "all"}:
        return value
                                                                           
    return "entry" if platform == "alipay" else "official"

def direct_file_upload_args(platform: str | None = None) -> bool:
    return platform in {"douyin", "wechat"}

def include_wrapper_sinks(platform: str | None = None) -> bool:
    if os.getenv("FLOW_ANALYSIS_INCLUDE_WRAPPER_SINKS", "").strip().lower() in {"1", "true", "yes", "y"}:
        return True
                                                         
    return platform == "alipay"

def include_rare_sinks(platform: str | None = None) -> bool:
    if os.getenv("FLOW_ANALYSIS_INCLUDE_RARE_SINKS", "").strip().lower() in {"1", "true", "yes", "y"}:
        return True
    return platform == "alipay"

def sink_flow_type_map(platform: str | None = None) -> dict[str, str]:
    sinks = dict(_CORE_SINK_FLOW_TYPE)
    if include_wrapper_sinks(platform):
        sinks.update(_WRAPPER_SINK_FLOW_TYPE)
    if include_rare_sinks(platform):
        sinks.update(_RARE_SINK_FLOW_TYPE)
    return sinks

@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    api_prefix: str
    source_apis: frozenset[str]
    source_methods: frozenset[str]
    sink_methods: frozenset[str]

    def flow_type_for(self, method: str) -> str | None:
        return _SINK_FLOW_TYPE.get(method)

@lru_cache(maxsize=None)
def load_spec(platform: str) -> PlatformSpec:
    prefix = API_PREFIX[platform]
    data = json.loads(api_dict_path(platform).read_text(encoding="utf-8"))
    source_apis: set[str] = set()
    for category in data.get("privacy_categories", []):
        for api in category.get("apis", []):
            if api.get("kind") != "api":
                continue
            name = api.get("name", "")
            if name.startswith(prefix):
                source_apis.add(name)
    source_apis.update(EXTRA_SOURCE_APIS.get(platform, set()))
    sink_methods = frozenset(sink_flow_type_map(platform).keys())
    source_methods = frozenset(api.split(".")[-1] for api in source_apis)
    return PlatformSpec(
        platform=platform,
        api_prefix=prefix,
        source_apis=frozenset(source_apis),
        source_methods=source_methods,
        sink_methods=sink_methods,
    )