"""Materialize the race/class dataset in Harbor's native task format."""

import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def generate(output, image="agent-wow-harbor:dev", race=None, character_class=None):
    characters = json.loads((ROOT / "characters.json").read_text())
    starting_areas = json.loads((ROOT / "starting_areas.json").read_text())
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
        area = starting_areas[r]
        name = f"{r}-{c}-starting-zone"
        task = Path(output) / name
        shutil.copytree(ROOT / "template", task, dirs_exist_ok=True)
        (task / "instruction.md").write_text((ROOT / "template/instruction.md").read_text().format(
            race=r.replace("-", " "), character_class=c, starting_area=area["name"]))
        for relative in ("task.toml", "environment/docker-compose.yaml"):
            path = task / relative
            path.write_text(path.read_text().replace("__TASK_NAME__", name).replace(
                "__AGENT_IMAGE__", json.dumps(image)))
        goal = {"race": r, "class": c, "starting_area": area["name"],
                "completion_quest_ids": area["completion_quest_ids"]}
        (task / "tests/goal.json").write_text(json.dumps(goal) + "\n")
        tasks.append(task)
    return tasks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image", default="agent-wow-harbor:dev")
    parser.add_argument("--race")
    parser.add_argument("--class", dest="character_class")
    args = parser.parse_args()
    output = args.output or ROOT / "tasks"
    try:
        tasks = generate(output, args.image, args.race, args.character_class)
    except ValueError as error:
        parser.error(str(error))
    print(f"Generated {len(tasks)} Harbor tasks in {output}")
