import json
from pathlib import Path


def normalize(value):
    return value.lower().replace("-", " ")


def grade_level(characters, goal, race, character_class):
    target = goal["level"]
    if type(target) is not int or not 1 <= target <= 80:
        raise ValueError("level target must be an integer between 1 and 80")
    completed = False
    for character in characters:
        level = character["level"]
        if type(level) is not int or not 1 <= level <= 80:
            raise ValueError("character level must be an integer between 1 and 80")
        matches = normalize(character["race"]) == race and normalize(character["class"]) == character_class
        completed = completed or (matches and level >= target)
    return float(completed)


def grade(characters, goal):
    if not isinstance(characters, list):
        raise TypeError("characters must be a list")
    race, character_class = normalize(goal["race"]), normalize(goal["class"])
    if "level" in goal and "completion_quest_ids" not in goal:
        return grade_level(characters, goal, race, character_class)
    quest_ids = goal["completion_quest_ids"]
    if not isinstance(quest_ids, list) or not quest_ids or any(
        type(quest) is not int or not 0 < quest <= 0xFFFFFFFF for quest in quest_ids
    ):
        raise ValueError("completion_quest_ids must be a non-empty list of positive uint32 quest IDs")
    accepted_quests = set(quest_ids)
    completed = False
    for character in characters:
        quests = character["completed_quest_ids"]
        if not isinstance(quests, list) or any(
            type(quest) is not int or not 0 < quest <= 0xFFFFFFFF for quest in quests
        ):
            raise TypeError("completed_quest_ids must be a list of positive uint32 quest IDs")
        matches = (normalize(character["race"]) == race
                   and normalize(character["class"]) == character_class)
        completed = completed or (matches and not accepted_quests.isdisjoint(quests))
    return float(completed)


if __name__ == "__main__":
    # Missing or malformed evidence raises an error, rather than scoring a failure
    # to collect evidence as a failed gameplay attempt.
    evidence = json.loads(Path("/evidence/characters.json").read_text())
    goal = json.loads(Path("/tests/goal.json").read_text())
    reward = grade(evidence["characters"], goal)
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "reward.txt").write_text(f"{reward}\n")
