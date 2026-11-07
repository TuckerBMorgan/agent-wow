import json
from pathlib import Path


def grade(characters, goal):
    normalize = lambda value: value.lower().replace("-", " ")
    quest_ids = goal["completion_quest_ids"]
    if not isinstance(quest_ids, list) or not quest_ids or any(
        type(quest) is not int or not 0 < quest <= 0xFFFFFFFF for quest in quest_ids
    ):
        raise ValueError("completion_quest_ids must be a non-empty list of positive uint32 quest IDs")
    accepted_quests = set(quest_ids)
    if not isinstance(characters, list):
        raise TypeError("characters must be a list")
    race, character_class = normalize(goal["race"]), normalize(goal["class"])
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
