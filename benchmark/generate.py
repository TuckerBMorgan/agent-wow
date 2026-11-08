"""Materialize the race/class dataset in Harbor's native task format."""

import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
LEVEL_SNAPSHOT = (
    'mkdir -p /evidence || exit 1; attempt=0; while [ "$attempt" -lt 10 ]; do '
    "if agent-wow auth login --timeout 10s >/dev/null && "
    "agent-wow char list --json --timeout 10s > /evidence/characters.tmp; then "
    "mv /evidence/characters.tmp /evidence/characters.json; exit $?; fi; "
    "attempt=$((attempt + 1)); sleep 3; done; exit 1"
)


def generate(output, image="agent-wow-harbor:dev", race=None, character_class=None,
             *, benchmark="starting-zone"):
    if benchmark not in ("starting-zone", "level-10"):
        raise ValueError(f"Unknown benchmark: {benchmark}")
    characters = json.loads((ROOT / "characters.json").read_text())
    level_target = benchmark == "level-10"
    if not level_target:
        starting_areas = json.loads((ROOT / "starting_areas.json").read_text())
        instruction_template = (ROOT / "template/instruction.md").read_text()
    snapshot = LEVEL_SNAPSHOT if level_target else "snapshot-characters"
    cases = [(r, c) for r, classes in characters.items() for c in classes
             if (race is None or r == race) and (character_class is None or c == character_class)]
    if not cases:
        raise ValueError("No matching race/class combination")
    output = Path(output)
    marker = output / ".generated"
    if output.exists():
        if not marker.is_file():
            raise ValueError(f"Refusing to replace an unmanaged directory: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    marker.touch()
    tasks = []
    for r, c in cases:
        race_name = r.replace("-", " ")
        goal = {"race": r, "class": c}
        if level_target:
            goal["level"] = 10
            instruction = f"create a {race_name} {c} and adventure until you are level 10\n"
        else:
            area = starting_areas[r]
            goal.update(starting_area=area["name"], completion_quest_ids=area["completion_quest_ids"])
            instruction = instruction_template.format(race=race_name, character_class=c, starting_area=area["name"])
        name = f"{r}-{c}-{benchmark}"
        task = Path(output) / name
        shutil.copytree(ROOT / "template", task, dirs_exist_ok=True)
        (task / "instruction.md").write_text(instruction)
        for relative in ("task.toml", "environment/docker-compose.yaml"):
            path = task / relative
            path.write_text(path.read_text().replace("__TASK_NAME__", name)
                            .replace("__AGENT_IMAGE__", json.dumps(image))
                            .replace("__SNAPSHOT_COMMAND__", json.dumps(snapshot)))
        (task / "tests/goal.json").write_text(json.dumps(goal) + "\n")
        tasks.append(task)
    return tasks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=("starting-zone", "level-10"), default="starting-zone")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image", default="agent-wow-harbor:dev")
    parser.add_argument("--race")
    parser.add_argument("--class", dest="character_class")
    args = parser.parse_args()
    output = args.output or ROOT / ("tasks-level-10" if args.benchmark == "level-10" else "tasks")
    try:
        tasks = generate(output, args.image, args.race, args.character_class, benchmark=args.benchmark)
    except ValueError as error:
        parser.error(str(error))
    print(f"Generated {len(tasks)} Harbor tasks in {output}")
