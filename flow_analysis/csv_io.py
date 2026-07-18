import csv
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDS = (
    "platform", "app_id", "source_file", "source_api", "source_loc",
    "sink_file", "sink_api", "sink_loc", "startpoint_type", "flow_type",
)

def normalize_startpoint_type(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() == "ui":
        return "ui"
    return "api.asyn"

@dataclass(frozen=True)
class TaintChain:
    platform: str
    app_id: str
    source_file: str
    source_api: str
    source_loc: str
    sink_file: str
    sink_api: str
    sink_loc: str
    startpoint_type: str
    flow_type: str

    @classmethod
    def from_witness(cls, platform: str, app_id: str, w: dict) -> "TaintChain":
        return cls(
            platform=platform,
            app_id=app_id,
            source_file=w.get("source_file", ""),
            source_api=w.get("source_api", ""),
            source_loc=w.get("source_loc", ""),
            sink_file=w.get("sink_file", ""),
            sink_api=w.get("sink_api", ""),
            sink_loc=w.get("sink_loc", ""),
            startpoint_type=normalize_startpoint_type(w.get("startpoint_type", "api.asyn")),
            flow_type=w.get("flow_type", ""),
        )

def write_csv(chains: list[TaintChain], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(FIELDS))
        writer.writeheader()
        for chain in chains:
            writer.writerow(asdict(chain))

def read_csv(path: Path) -> list[TaintChain]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        chains = []
        for row in csv.DictReader(f):
            data = {k: row.get(k, "") for k in FIELDS}
            data["startpoint_type"] = normalize_startpoint_type(data.get("startpoint_type"))
            chains.append(TaintChain(**data))
        return chains
