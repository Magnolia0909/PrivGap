import os
import re
from typing import List, Optional
import logging

TOKEN_RE = re.compile(r"[a-zA-Z0-9\u4e00-\u9fa5]+")
logger = logging.getLogger(__name__)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.WARNING)
logging.getLogger("torch").setLevel(logging.WARNING)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_cached_model = None
_model_load_error = None
_model_load_error_logged = False
_semantic_error_logged = False

class SentenceModelUnavailable(RuntimeError):
    pass

def compact(text: str) -> str:
    return "".join(TOKEN_RE.findall(text))

def get_sentence_model(model_path: Optional[str] = None):
    global _cached_model, _model_load_error, _model_load_error_logged

    if _cached_model is not None:
        return _cached_model
    if _model_load_error is not None:
        raise SentenceModelUnavailable(f"SentenceTransformer model is unavailable: {_model_load_error}") from _model_load_error

    try:
        from sentence_transformers import SentenceTransformer

        if model_path is None:
            try:
                from config.config import Config
                config = Config()
                model_path = config.huggingface_sentence_transformer_model
            except Exception as e:
                logger.warning(f"Unable to read the model path from config: {e}")
                model_path = None

        if model_path:
            logger.debug(f"Loading SentenceTransformer model: {model_path}")
            _cached_model = SentenceTransformer(model_path, local_files_only=True)
        else:
            logger.warning("Using the default model, which may require a download")
            _cached_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

        return _cached_model

    except Exception as e:
        _model_load_error = e
        if not _model_load_error_logged:
            logger.error(f"Failed to load the SentenceTransformer model; semantic similarity will fall back to zero: {e}")
            _model_load_error_logged = True
        raise SentenceModelUnavailable(f"SentenceTransformer model is unavailable: {e}") from e

def semantic_similarity_batch(query: str, candidates: List[str], model_path: Optional[str] = None) -> List[float]:
    global _semantic_error_logged

    if not query or not candidates:
        return [0.0] * len(candidates)

    try:
        from sentence_transformers import util
        model = get_sentence_model(model_path)
        query_embedding = model.encode(query, convert_to_tensor=True)
        candidate_embeddings = model.encode(candidates, convert_to_tensor=True)
        similarities = util.cos_sim(query_embedding, candidate_embeddings)
        return similarities[0].cpu().tolist()

    except SentenceModelUnavailable:
        return [0.0] * len(candidates)
    except Exception as e:
        if not _semantic_error_logged:
            logger.error(f"Semantic similarity failed; subsequent scores will fall back to zero: {e}")
            _semantic_error_logged = True
        return [0.0] * len(candidates)
