import copy
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.similarity import SentenceModelUnavailable, compact, get_sentence_model, semantic_similarity_batch

def _allow_substring_fuzzy(ontology_term: str, raw_term: str) -> bool:
    ontology_term = str(ontology_term or "").strip()
    raw_term = str(raw_term or "").strip()
    if not ontology_term or not raw_term:
        return False
    if ontology_term in GENERIC_SUBSTRING_TERMS:
        return False
    if len(ontology_term) < 3:
        return False
    return raw_term in ontology_term or ontology_term in raw_term

@dataclass
class OntologyMatch:
    raw_term: str
    matched_node: str
    hierarchy_path: str
    node_id: str
    legal_basis: str = ""
    interface: List[str] = field(default_factory=list)
    match_type: str = "exact"
    scope: str = ""
    api_bound: bool = False

class OntologyNormalizer:
    def __init__(self, ontology_base):
        self.kb = ontology_base

        self.term_to_node = self.kb.term_to_node
        self.data_types = self.kb.data_types

        self.use_semantic_fallback = bool(getattr(self.kb.config, "use_semantic_ontology_fallback", False))
        self.semantic_threshold = float(getattr(self.kb.config, "ontology_semantic_threshold", 0.82))
        self.semantic_api_threshold = float(getattr(self.kb.config, "ontology_semantic_api_threshold", 0.88))
        self.semantic_margin = float(getattr(self.kb.config, "ontology_semantic_margin", 0.05))
        self.semantic_top_k = int(getattr(self.kb.config, "ontology_semantic_top_k", 5))
        self._semantic_candidates = None
        self._semantic_candidate_labels = None
        self._semantic_candidate_infos = None
        self._semantic_candidate_embeddings = None
        self._semantic_query_cache = {}
        self._semantic_fallback_unavailable = False
        self._supplement_parent_candidates = None
        self._supplement_parent_labels = None
        self._supplement_parent_infos = None
        self._supplement_parent_embeddings = None
        self._supplement_parent_query_cache = {}
        self._supplement_parent_semantic_unavailable = False

    def normalize_data_type(self, raw_term: str) -> Optional[OntologyMatch]:
        if not raw_term or not raw_term.strip():
            return None

        term = raw_term.strip()

        if term in self.term_to_node:
            node_info = self.term_to_node[term]
            return self._match_from_node(raw_term, node_info, "exact")

        alias_path = DOMAIN_SYNONYM_LEXICON.get(term)
        if alias_path:
            node_info = self._node_info_for_path(alias_path)
            if node_info:
                return self._match_from_node(raw_term, node_info, "alias")

        for ontology_term, node_info in self.term_to_node.items():
            if _allow_substring_fuzzy(ontology_term, term):
                return self._match_from_node(raw_term, node_info, "fuzzy")

        for hierarchy_path, terms in self.data_types.items():
            for t in terms:
                if term == t or _allow_substring_fuzzy(t, term):
                    node_info = self._node_info_for_path(hierarchy_path)
                    if node_info:
                        return self._match_from_node(raw_term, node_info, "parent")

        if self.use_semantic_fallback:
            semantic_match = self._semantic_classify(term)
            if semantic_match:
                return semantic_match
        return None

    def _match_from_node(self, raw_term: str, node_info: dict, match_type: str) -> OntologyMatch:
        return OntologyMatch(
            raw_term=raw_term,
            matched_node=node_info["node_name"],
            hierarchy_path=node_info["hierarchy_path"],
            node_id=node_info["id"],
            legal_basis=node_info.get("legal_basis", ""),
            interface=node_info.get("interface", []),
            match_type=match_type,
            scope=node_info.get("scope", ""),
            api_bound=bool(node_info.get("api_bound", False)),
        )

    def _node_info_for_path(self, hierarchy_path: str) -> Optional[dict]:
        for node_info in self.term_to_node.values():
            if node_info["hierarchy_path"] == hierarchy_path:
                return node_info
        return None

    def _semantic_classify(self, term: str) -> Optional[OntologyMatch]:
        if self._semantic_fallback_unavailable:
            return None
        try:
            labels, infos, candidate_embeddings = self._get_semantic_candidate_embeddings()
            if not labels:
                return None
            if term in self._semantic_query_cache:
                scores = self._semantic_query_cache[term]
            else:
                from sentence_transformers import util

                model = get_sentence_model(getattr(self.kb.config, "huggingface_sentence_transformer_model", None))
                query_embedding = model.encode(term, convert_to_tensor=True)
                scores = util.cos_sim(query_embedding, candidate_embeddings)[0].cpu().tolist()
                self._semantic_query_cache[term] = scores
        except SentenceModelUnavailable:
            self._semantic_fallback_unavailable = True
            return None
        except Exception:
            self._semantic_fallback_unavailable = True
            return None

        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        if not ranked:
            return None
        top_idx, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        node_info = infos[top_idx]
        threshold = self.semantic_api_threshold if bool(node_info.get("api_bound", False)) else self.semantic_threshold
        if top_score < threshold or (top_score - second_score) < self.semantic_margin:
            return None
        return self._match_from_node(term, node_info, "semantic")

    def _get_semantic_candidate_embeddings(self):
        if self._semantic_candidate_embeddings is not None:
            return self._semantic_candidate_labels, self._semantic_candidate_infos, self._semantic_candidate_embeddings
        candidates = self._get_semantic_candidates()
        self._semantic_candidate_labels = [c[0] for c in candidates]
        self._semantic_candidate_infos = [c[1] for c in candidates]
        if not self._semantic_candidate_labels:
            self._semantic_candidate_embeddings = []
            return self._semantic_candidate_labels, self._semantic_candidate_infos, self._semantic_candidate_embeddings
        model = get_sentence_model(getattr(self.kb.config, "huggingface_sentence_transformer_model", None))
        self._semantic_candidate_embeddings = model.encode(self._semantic_candidate_labels, convert_to_tensor=True)
        return self._semantic_candidate_labels, self._semantic_candidate_infos, self._semantic_candidate_embeddings

    def _get_semantic_candidates(self) -> List[tuple]:
        if self._semantic_candidates is not None:
            return self._semantic_candidates
        candidates = []
        seen = set()
        for ontology_term, node_info in self.term_to_node.items():
            term = str(ontology_term or "").strip()
            if not term or len(term) < 2:
                continue
            if term in GENERIC_SEMANTIC_TARGETS:
                continue
            key = (term, node_info.get("hierarchy_path"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((term, node_info))
        self._semantic_candidates = candidates
        return candidates

    def normalize_to_top_level_supplement(self, raw_term: str) -> Optional[OntologyMatch]:
        term = str(raw_term or "").strip()
        if not term:
            return None
        labels, infos = self._get_top_level_supplement_candidates()
        if not labels:
            return None
        scores = self._score_supplement_parent_candidates(term, labels)
        if not scores:
            return None
        top_idx = max(range(len(scores)), key=lambda i: scores[i])
        return self._match_from_node(term, infos[top_idx], "supplement_parent_fallback")

    def _score_supplement_parent_candidates(self, term: str, labels: List[str]) -> List[float]:
        if term in self._supplement_parent_query_cache:
            return self._supplement_parent_query_cache[term]

        scores = None
        if not self._supplement_parent_semantic_unavailable:
            try:
                from sentence_transformers import util

                model = get_sentence_model(getattr(self.kb.config, "huggingface_sentence_transformer_model", None))
                if self._supplement_parent_embeddings is None:
                    self._supplement_parent_embeddings = model.encode(labels, convert_to_tensor=True)
                query_embedding = model.encode(term, convert_to_tensor=True)
                scores = util.cos_sim(query_embedding, self._supplement_parent_embeddings)[0].cpu().tolist()
            except SentenceModelUnavailable:
                self._supplement_parent_semantic_unavailable = True
            except Exception:
                self._supplement_parent_semantic_unavailable = True

        if scores is None:
            scores = [self._lexical_similarity(term, label) for label in labels]
        self._supplement_parent_query_cache[term] = scores
        return scores

    def _get_top_level_supplement_candidates(self):
        if self._supplement_parent_candidates is not None:
            return self._supplement_parent_labels, self._supplement_parent_infos

        root = (self.kb.data_types_ontology or {}).get("补充隐私数据类型")
        candidates = []
        if isinstance(root, dict):
            for label, node in (root.get("children") or {}).items():
                if not isinstance(node, dict):
                    continue
                node_info = self._node_info_for_path(f"补充隐私数据类型 > {label}")
                if not node_info:
                    node_info = {
                        "id": node.get("id", ""),
                        "hierarchy_path": f"补充隐私数据类型 > {label}",
                        "node_name": label,
                        "scope": node.get("scope", "policy_supplement"),
                        "api_bound": bool(node.get("api_bound", False)),
                    }
                terms = [str(t).strip() for t in node.get("terms", []) if str(t).strip()]
                label_text = " ".join([label] + terms[:8])
                candidates.append((label_text, node_info))

        self._supplement_parent_candidates = candidates
        self._supplement_parent_labels = [c[0] for c in candidates]
        self._supplement_parent_infos = [c[1] for c in candidates]
        return self._supplement_parent_labels, self._supplement_parent_infos

    @staticmethod
    def _lexical_similarity(a: str, b: str) -> float:
        ca = compact(a)
        cb = compact(b)
        if not ca or not cb:
            return 0.0
        return SequenceMatcher(None, ca, cb, autojunk=False).ratio()

    def is_ignorable_data_type(self, raw_term: str) -> bool:
        term = str(raw_term or "").strip()
        if not term:
            return True
        if term in IGNORABLE_DATA_TYPE_TERMS:
            return True
        if len(term) > 18 and any(hint in term for hint in ("相关", "必要", "提供的", "共享的")):
            return True
        return False

    def normalize_batch(self, privacy_items: List[Dict]) -> List[Dict]:
        normalized_items = []
        for item in privacy_items:
            data_type = item.get("data_type", "")
            match = self.normalize_data_type(data_type)
            item_copy = copy.deepcopy(item)
            if match:
                item_copy["ontology_match"] = {
                    "matched_node": match.matched_node,
                    "hierarchy_path": match.hierarchy_path,
                    "node_id": match.node_id,
                    "legal_basis": match.legal_basis,
                    "interface": match.interface,
                    "match_type": match.match_type,
                    "scope": match.scope,
                    "api_bound": match.api_bound,
                }
            else:
                item_copy["ontology_match"] = None
            normalized_items.append(item_copy)
        return normalized_items

    def group_by_hierarchy(self, privacy_items: List[Dict]) -> Dict[str, List[Dict]]:
        grouped = {}
        for item in privacy_items:
            ontology_match = item.get("ontology_match")
            hierarchy_path = ontology_match["hierarchy_path"] if ontology_match else "其他"
            grouped.setdefault(hierarchy_path, []).append(item)
        return grouped

    def get_statistics(self, privacy_items: List[Dict]) -> Dict[str, Any]:
        total = len(privacy_items)
        matched = sum(1 for item in privacy_items if item.get("ontology_match"))
        unmatched = total - matched
        exact_matches = sum(
            1 for item in privacy_items
            if item.get("ontology_match") and item["ontology_match"].get("match_type") == "exact"
        )
        fuzzy_matches = sum(
            1 for item in privacy_items
            if item.get("ontology_match") and item["ontology_match"].get("match_type") == "fuzzy"
        )
        parent_matches = sum(
            1 for item in privacy_items
            if item.get("ontology_match") and item["ontology_match"].get("match_type") == "parent"
        )
        semantic_matches = sum(
            1 for item in privacy_items
            if item.get("ontology_match") and item["ontology_match"].get("match_type") == "semantic"
        )
        grouped = self.group_by_hierarchy(privacy_items)
        return {
            "total": total,
            "matched": matched,
            "unmatched": unmatched,
            "match_rate": matched / total if total > 0 else 0,
            "exact_matches": exact_matches,
            "fuzzy_matches": fuzzy_matches,
            "parent_matches": parent_matches,
            "semantic_matches": semantic_matches,
            "categories": {path: len(items) for path, items in grouped.items()},
        }

    def is_hierarchical_match(self, path1: str, path2: str) -> bool:
        if path1 == path2:
            return True
        parts1 = path1.split(" > ")
        parts2 = path2.split(" > ")
        if len(parts1) < len(parts2) and parts2[:len(parts1)] == parts1:
            return True
        if len(parts2) < len(parts1) and parts1[:len(parts2)] == parts2:
            return True
        return False

    def get_parent_path(self, hierarchy_path: str) -> Optional[str]:
        parts = hierarchy_path.split(" > ")
        if len(parts) <= 1:
            return None
        return " > ".join(parts[:-1])

    def get_leaf_node(self, hierarchy_path: str) -> str:
        return hierarchy_path.split(" > ")[-1]

    def _refine_to_child_node(self, term: str, parent_node_info: dict) -> Optional[dict]:
        try:
            parent_path = parent_node_info["hierarchy_path"]
            child_terms = [
                t for t, info in self.term_to_node.items()
                if info["hierarchy_path"].startswith(parent_path + " > ")
            ]
            if not child_terms:
                return None
            child_similarities = semantic_similarity_batch(term, child_terms)
            max_idx = max(range(len(child_similarities)), key=lambda i: child_similarities[i])
            if child_similarities[max_idx] >= 0.65:
                return self.term_to_node[child_terms[max_idx]]
        except Exception:
            pass
        return None
