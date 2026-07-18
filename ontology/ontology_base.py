from __future__ import annotations
import json
import logging
import os
from typing import Dict, List

class OntologyBase:
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.ontology_path = os.path.join(self.config.DATA_DIR, self.config.ontology_base_file)
        self.ontology: Dict = {}
        self.data_types: Dict[str, List[str]] = {}
        self.data_types_ontology: Dict = {}
        self.term_to_node: Dict[str, dict] = {}
        self.recipients: List[str] = []
        self._load_ontology()

    def _load_ontology(self) -> None:
        if not os.path.exists(self.ontology_path):
            raise FileNotFoundError(f"Ontology file not found: {self.ontology_path}")
        with open(self.ontology_path, "r", encoding="utf-8") as f:
            self.ontology = json.load(f) or {}
        self.recipients = self.ontology.get("recipients", [])
        self.data_types_ontology = self.ontology.get("data_types_ontology", {})
        if self.data_types_ontology:
            self._flatten_ontology()
        self.logger.info(
            "Loaded ontology: terms=%s, categories=%s, file=%s",
            len(self.term_to_node),
            len(self.data_types),
            self.ontology_path,
        )

    def _flatten_ontology(self) -> None:
        def traverse(node_dict, parent_path="", parent_id="") -> None:
            for node_name, node_info in node_dict.items():
                if not isinstance(node_info, dict):
                    continue
                current_path = f"{parent_path} > {node_name}" if parent_path else node_name
                node_id = node_info.get("id", "")
                terms = node_info.get("terms", [])
                children = node_info.get("children", {})
                is_leaf = len(children) == 0
                depth = len(current_path.split(" > "))
                if terms:
                    bucket = self.data_types.setdefault(current_path, [])
                    for term in terms:
                        if term not in bucket:
                            bucket.append(term)
                        info = {
                            "id": node_id,
                            "hierarchy_path": current_path,
                            "node_name": node_name,
                            "parent_id": parent_id,
                            "is_leaf": is_leaf,
                            "depth": depth,
                            "canonical_id": node_info.get("canonical_id", node_id),
                            "scope": node_info.get("scope", ""),
                            "api_bound": bool(node_info.get("api_bound", False)),
                            "api_bound_by_platform": node_info.get("api_bound_by_platform", {}),
                        }
                        existing = self.term_to_node.get(term)
                        if existing and existing.get("scope") == "guideline_governed" and info.get("scope") != "guideline_governed":
                            continue
                        self.term_to_node[term] = info

                if children:
                    traverse(children, current_path, node_id)
        traverse(self.data_types_ontology)

    def find_category(self, term: str) -> str:
        t = (term or "").strip()
        if not t:
            raise ValueError("[OntologyBase.find_category] empty term")
        if t in self.term_to_node:
            return self.term_to_node[t]["hierarchy_path"]
        for category, terms in self.data_types.items():
            if t in terms:
                return category
        return "Other"

    def get_children_categories(self, parent_id: str) -> List[str]:
        if not parent_id:
            return list(self.data_types.keys())
        children_paths = []
        for info in self.term_to_node.values():
            if info.get("parent_id") == parent_id:
                children_paths.append(info["hierarchy_path"])
        unique_paths = sorted(set(children_paths))
        if unique_paths:
            return unique_paths
        for info in self.term_to_node.values():
            if info.get("id") == parent_id:
                return [info["hierarchy_path"]]
        return []

    def normalize_term(self, term: str) -> str:
        return (term or "").strip()
