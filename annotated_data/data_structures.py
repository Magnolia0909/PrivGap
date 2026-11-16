from dataclasses import dataclass, field, asdict
from typing import List, Dict

@dataclass
class PrivacyItem:
    data_type: str = ""
    recipients: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    def get_key(self) -> str:
        return f"{self.data_type}"

@dataclass
class ExtractionResult:
    app_id: str
    privacy_items: List[PrivacyItem]
    total_sentences: int
    processed_sentences: int
    extraction_time: float
    model_calls: int

    def to_dict(self) -> Dict:
        return {
            'app_id': self.app_id,
            'privacy_items': [item.to_dict() for item in self.privacy_items],
            'extraction_time': self.extraction_time,
            'model_calls': self.model_calls,
            'summary': {
                'total_privacy_items': len(self.privacy_items),
                'data_types': list(set(item.data_type for item in self.privacy_items if item.data_type)),
            }
        }
