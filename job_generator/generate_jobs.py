import argparse
import json
from pathlib import Path

from job_generator import create_job_definitions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", action="store_true",
                        help="Generate only the first character, run, evaluation, and trial.")
    args = parser.parse_args()
    script_dir = Path(__file__).resolve().parent
    config = json.loads((script_dir / "benchmark.json").read_text())
    if args.single:
        race, classes = next(iter(config["characters"].items()))
        config["characters"] = {race: classes[:1]}
        for key in ("runs", "evaluations", "trial_ids"):
            config[key] = config[key][:1]
    output = script_dir.parent / "tmp" / "participants"

    files = create_job_definitions(
        config,
        table_name="agent-wow-jobs",
        output_dir=str(output),
    )

    print(f"Created {len(files)} participant files in {output}")
    if args.single:
        print(files[0])


if __name__ == "__main__":
    main()
