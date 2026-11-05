import json
from pathlib import Path


def grade(characters, goal):
    normalize = lambda value: value.lower().replace("-", " ")
    return float(any(
        normalize(character["race"]) == normalize(goal["race"])
        and normalize(character["class"]) == normalize(goal["class"])
        and character["level"] >= goal["level"]
        for character in characters
    ))


if __name__ == "__main__":
    # Missing or malformed evidence raises an error, rather than scoring a failure
    # to collect evidence as a failed gameplay attempt.
    evidence = json.loads(Path("/evidence/characters.json").read_text())
    goal = json.loads(Path("/tests/goal.json").read_text())
    reward = grade(evidence["characters"], goal)
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "reward.txt").write_text(f"{reward}\n")
