import json
from pathlib import Path
import tempfile
import unittest

from harbor.models.task.task import Task

from generate import generate
from template.tests.grade import grade


class GradingTests(unittest.TestCase):
    goal = {"race": "night-elf", "class": "druid", "completion_quest_ids": [928, 921]}

    def character(self, **changes):
        return {"race": "Night Elf", "class": "Druid", "level": 6,
                "completed_quest_ids": [456, 928]} | changes

    def test_requires_the_requested_race_class_and_rewarded_completion_quest(self):
        for changes in ({"race": "Human"}, {"class": "Priest"},
                        {"completed_quest_ids": []}, {"completed_quest_ids": [456]}):
            with self.subTest(changes=changes):
                self.assertEqual(grade([self.character(**changes)], self.goal), 0)
        self.assertEqual(grade([self.character()], self.goal), 1)

    def test_either_completion_quest_is_sufficient(self):
        for quests in ([928], [921], [921, 928]):
            with self.subTest(quests=quests):
                self.assertEqual(grade([self.character(completed_quest_ids=quests)], self.goal), 1)
        for changes in ({"race": "Human"}, {"class": "Priest"}):
            with self.subTest(changes=changes):
                self.assertEqual(grade([self.character(completed_quest_ids=[921], **changes)], self.goal), 0)

    def test_accepted_quests_do_not_count_as_rewarded_quests(self):
        character = self.character(completed_quest_ids=[456], quest_log=[921, 928])
        self.assertEqual(grade([character], self.goal), 0)

    def test_level_does_not_determine_completion(self):
        self.assertEqual(grade([self.character(level=1)], self.goal), 1)
        self.assertEqual(grade([self.character(level=80, completed_quest_ids=[])], self.goal), 0)
        character = self.character()
        del character["level"]
        self.assertEqual(grade([character], self.goal), 1)

    def test_quests_must_belong_to_the_matching_character(self):
        for quest in self.goal["completion_quest_ids"]:
            with self.subTest(quest=quest):
                self.assertEqual(grade([
                    self.character(race="Human", completed_quest_ids=[quest]),
                    self.character(completed_quest_ids=[])
                ], self.goal), 0)
        self.assertEqual(grade([
            self.character(race="Human", level=80), self.character()
        ], self.goal), 1)
        self.assertEqual(grade([], self.goal), 0)

    def test_bad_evidence_does_not_silently_become_a_gameplay_failure(self):
        with self.assertRaises(KeyError):
            grade([{"race": "Night Elf", "class": "Druid"}], self.goal)
        for quests in (None, "928", ["928"], [True], [0], [-1], [0x100000000]):
            with self.subTest(quests=quests), self.assertRaises(TypeError):
                grade([self.character(completed_quest_ids=quests)], self.goal)
        with self.assertRaises(TypeError):
            grade(None, self.goal)
        # A valid first character must not hide a failed later snapshot.
        with self.assertRaises(KeyError):
            grade([self.character(), {"race": "Human", "class": "Warrior"}], self.goal)

    def test_goal_requires_a_nonempty_list_of_valid_quest_ids(self):
        for quest_ids in (None, "928", 928, [], [None], ["928"], [True], [0], [-1], [0x100000000], [928, 0]):
            with self.subTest(quest_ids=quest_ids), self.assertRaises(ValueError):
                grade([], self.goal | {"completion_quest_ids": quest_ids})


class DatasetTests(unittest.TestCase):
    starting_areas = {
        "human": ("Northshire Valley", [54, 6]),
        "orc": ("Valley of Trials", [805, 794]),
        "dwarf": ("Coldridge Valley", [420, 218]),
        "night-elf": ("Shadowglen", [928, 921]),
        "undead": ("Deathknell", [383, 382]),
        "tauren": ("Red Cloud Mesa", [763, 757]),
        "gnome": ("Coldridge Valley", [420, 218]),
        "troll": ("Valley of Trials", [805, 794]),
        "blood-elf": ("Sunstrider Isle", [8350, 8347]),
        "draenei": ("Ammen Vale", [9313, 9312]),
    }

    def test_entire_matrix_loads_as_native_harbor_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            tasks = generate(Path(directory) / "tasks")
            self.assertEqual(len(tasks), 52)
            for path in tasks:
                with self.subTest(task=path.name):
                    task = Task(path)
                    self.assertEqual(task.config.agent.timeout_sec, 21600)
                    self.assertEqual(task.config.verifier.environment_mode.value, "separate")
                    self.assertIsNone(task.config.verifier.environment.healthcheck)
                    self.assertEqual(task.config.verifier.environment.workdir, "/")
                    snapshot = next(a for a in task.config.artifacts if a.source == "/evidence/characters.json")
                    self.assertEqual(snapshot.service, "observer")
                    goal = json.loads((path / "tests/goal.json").read_text())
                    area, quest_ids = self.starting_areas[goal["race"]]
                    self.assertEqual(goal["starting_area"], area)
                    self.assertEqual(goal["completion_quest_ids"], quest_ids)
                    self.assertNotIn("handoff_quest_id", goal)
                    self.assertNotIn("level", goal)
                    race = goal["race"].replace("-", " ")
                    self.assertEqual((path / "instruction.md").read_text(),
                                     f"Create a {race} {goal['class']} and complete {area}.\n")

    def test_filtered_regeneration_does_not_leave_stale_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tasks"
            generate(output)
            tasks = generate(output, race="human", character_class="warrior")
            self.assertEqual(len(list(output.glob("*/task.toml"))), 1)
            self.assertEqual(tasks[0].name, "human-warrior-starting-zone")

    def test_generator_rejects_unknown_cases_and_unmanaged_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No matching"):
                generate(Path(directory) / "tasks", race="pandaren")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                generate(directory)


if __name__ == "__main__":
    unittest.main()
