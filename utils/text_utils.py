import re
import coloredlogs, logging

logger = logging.getLogger(__name__)

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;|&amp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def normalize_punctuation(text: str) -> str:
    if not text:
        return text
    mapping = {
        ",": "，",
        ";": "；",
        ":": "：",
        "?": "？",
        "!": "！",
        ".": "。",
    }
    for k, v in mapping.items():
        text = text.replace(k, v)
    return text

def extract_sentences(text: str) -> list:
    if not text:
        return []
    text = clean_text(normalize_punctuation(text))
    parts = re.split(r"[。！？；\n]", text)
    sentences = [p.strip() for p in parts if len(p.strip()) > 2]
    logger.debug(f"extract_sentences: {len(sentences)} sentences.")
    return sentences