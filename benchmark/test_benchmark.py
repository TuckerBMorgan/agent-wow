import json
from pathlib import Path
import tempfile
import unittest

from harbor.models.task.task import Task

from generate import generate
from template.tests.grade import grade


class GradingTests(unittest.TestCase):
    goal = {"race": "night-elf", "class": "druid", "level": 10}

    def character(self, **changes):
        return {"race": "Night Elf", "class": "Druid", "level": 10} | changes

    def test_requires_the_requested_race_class_and_level(self):
        for changes in ({"race": "Human"}, {"class": "Priest"}, {"level": 9}):
            with self.subTest(changes=changes):
                self.assertEqual(grade([self.character(**changes)], self.goal), 0)
        self.assertEqual(grade([self.character(level=11)], self.goal), 1)

    def test_looks_for_a_matching_character_instead_of_the_highest_level(self):
        self.assertEqual(grade([
            self.character(race="Human", level=80), self.character()
        ], self.goal), 1)
        self.assertEqual(grade([], self.goal), 0)

    def test_bad_evidence_does_not_silently_become_a_gameplay_failure(self):
        with self.assertRaises(KeyError):
            grade([{"level": 10}], self.goal)
        with self.assertRaises(TypeError):
            grade([self.character(level="10")], self.goal)


class DatasetTests(unittest.TestCase):
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
                    self.assertEqual(goal["level"], 10)

    def test_filtered_regeneration_does_not_leave_stale_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tasks"
            generate(output)
            tasks = generate(output, race="human", character_class="warrior")
            self.assertEqual(len(list(output.glob("*/task.toml"))), 1)
            self.assertEqual(tasks[0].name, "human-warrior-level-10")

    def test_generator_rejects_unknown_cases_and_unmanaged_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No matching"):
                generate(Path(directory) / "tasks", race="pandaren")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                generate(directory)


if __name__ == "__main__":
    unittest.main()
