import os
import json
import coloredlogs, logging
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple
from utils.html_processor import HTMLProcessor

class DataLoader:
    def __init__(self, config):
        self.config = config
        self.html_processor = HTMLProcessor()
        self.logger = logging.getLogger(__name__)
    
    def load_txt_files(self, input_dir: str) -> Dict[str, str]:
        if not os.path.exists(input_dir):
            self.logger.error(f"Input directory does not exist: {input_dir}")
            return {}

        txt_files = [f for f in os.listdir(input_dir) if f.endswith('.txt')]
        self.logger.info(f"Found {len(txt_files)} txt files to process")

        loaded_data = {}
        empty_files = []
        failed_files = []

        for filename in txt_files:
            file_path = os.path.join(input_dir, filename)
            app_id = os.path.splitext(filename)[0]
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content or len(content) < 10:
                    empty_files.append(app_id)
                    continue
                loaded_data[app_id] = content
            except Exception as e:
                failed_files.append(app_id)
                self.logger.error(f"Failed to read file {filename}: {e}")

        self.logger.info(
            f"Loading complete: {len(loaded_data)} succeeded, {len(empty_files)} empty files, {len(failed_files)} failed"
        )
        return loaded_data
    
    def convert_html_to_txt(self, html_dir: str, txt_output_dir: str) -> dict:
        return self.html_processor.batch_convert_html_directory(html_dir, txt_output_dir)
    
def load_ground_truth_entries(json_path: str) -> Iterator[Tuple[str, Set[str]]]:
    path = Path(json_path)
    with path.open("r", encoding="utf-8") as f:
        ground_truth_data = json.load(f)
    for item in ground_truth_data:
        app_id = (item or {}).get("app_id")
        if not app_id:
            continue
        data_types: Set[str] = set()
        for pi in (item or {}).get("privacy_items", []):
            dt = (pi or {}).get("data_type", "").strip()
            if dt:
                data_types.add(dt)
        yield app_id, data_types
