import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "skills/orchestrate/SKILL.md").read_text()

PLAN_SENTENCE = "Write the goal, the done criteria and what is out of scope at the top of that `plan.md`."
REPORT_SENTENCE = (
    "When `plan.md` exists, judge its done criteria against each code task's brief and worktree, "
    "including a task that is ready to merge and not yet on `HEAD`. A criterion that no brief states "
    "is one more table row with status `not started`. Do not start that row. Add no such row when "
    "every criterion is already stated in some brief. Skip this judgment while any planned code task "
    "is failed, blocked or still running, and name that task in the table."
)


class OrchestrateReportTest(unittest.TestCase):
    def test_sentences_present(self):
        self.assertEqual(SKILL.count(PLAN_SENTENCE), 1)
        self.assertIn(REPORT_SENTENCE, SKILL)

    def test_versions(self):
        self.assertEqual(re.search(r'version: "([^"]+)"', SKILL).group(1), "0.4.2")
        catalog = json.loads((ROOT / "site/catalog.json").read_text())
        entry = next(s for s in catalog["skills"] if s["id"] == "orchestrate")
        self.assertEqual(entry["version"], "0.4.2")

    def test_eval_case(self):
        evals = (ROOT / "skills/orchestrate/tests/eval.yaml").read_text()
        self.assertIn("- name: goal_gap_reported_not_started", evals)


if __name__ == "__main__":
    unittest.main()
