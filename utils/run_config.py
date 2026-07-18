import os
from config.config import Config

ENTITY_STAGE = "entity_extraction"
ONTOLOGY_STAGE = "ontology_consistency"

def get_run_stage(use_ontology: bool) -> str:
    return ONTOLOGY_STAGE if use_ontology else ENTITY_STAGE

def build_policy_config(run_tag: str, use_ontology: bool):
    cfg = Config()
    if use_ontology:
        cfg.OUTPUT_DIR = cfg.get_run_output_dir(get_run_stage(use_ontology), run_tag)
    else:
        cfg.OUTPUT_DIR = cfg.get_model_results_root(ENTITY_STAGE)
    cfg.FILTER_DIR = os.path.join(cfg.OUTPUT_DIR, "filter")
    cfg.INTERMEDIATE_DIR = os.path.join(cfg.OUTPUT_DIR, "intermediate")
    cfg.test_result_dir = os.path.join(cfg.OUTPUT_DIR, "extractions")
    cfg.use_ontology_normalization = use_ontology
    return cfg

def build_guideline_config(run_tag: str):
    cfg = Config()
    cfg.OUTPUT_DIR = cfg.get_stage_run_output_dir(ENTITY_STAGE, run_tag)
    cfg.FILTER_DIR = os.path.join(cfg.OUTPUT_DIR, "filter")
    cfg.INTERMEDIATE_DIR = os.path.join(cfg.OUTPUT_DIR, "intermediate")
    cfg.test_result_dir = os.path.join(cfg.OUTPUT_DIR, "extractions")
    cfg.use_ontology_normalization = False
    return cfg

def build_ontology_config(run_tag: str):
    cfg = Config()
    cfg.OUTPUT_DIR = cfg.get_run_output_dir(ONTOLOGY_STAGE, run_tag)
    cfg.FILTER_DIR = os.path.join(cfg.OUTPUT_DIR, "filter")
    cfg.INTERMEDIATE_DIR = os.path.join(cfg.OUTPUT_DIR, "intermediate")
    cfg.test_result_dir = os.path.join(cfg.OUTPUT_DIR, "extractions")
    cfg.use_ontology_normalization = True
    return cfg
