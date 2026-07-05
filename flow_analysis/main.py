from __future__ import annotations
import argparse
from flow_analysis.config import default_pred_csv
from flow_analysis.csv_io import write_csv
from flow_analysis.pipeline import run_platform

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["wechat", "alipay", "douyin"])
    parser.add_argument("--app", default=None)
    args = parser.parse_args()

    chains = run_platform(args.platform, app_id=args.app)
    pred_csv = default_pred_csv(args.platform)
    write_csv(chains, pred_csv)

if __name__ == "__main__":
    main()
