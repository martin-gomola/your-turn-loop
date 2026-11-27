import importlib.util
import json
import re
import shutil
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location("validate", ROOT / "scripts/validate.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / "repository"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", "tmp", "dist", "__pycache__", ".venv", ".DS_Store", ".delegate-code", ".second-opinion"))

    def test_skills_are_distributed_directly(self):
        validator.validate(self.root)
        self.assertEqual({p.name for p in (self.root / 'skills').iterdir()}, {'orchestrate', 'what-to-ask', 'your-turn-handoff', 'second-opinion', 'codex-image', 'delegate-code'})
        self.assertFalse(any((self.root / 'plugins').rglob('*SKILL.md')))
        catalog = json.loads((self.root / 'site/catalog.json').read_text())
        for skill in catalog['skills']:
            self.assertNotIn('plugin', skill)
            self.assertNotIn('pluginPath', skill)

    def test_installer_cannot_silently_select_another_skill(self):
        path = self.root / 'site/catalog.json'
        catalog = json.loads(path.read_text())
        for source in ('skills/your-turn-handoff', '../outside', 'skills/../README.md'):
            with self.subTest(source=source):
                catalog['skills'][0]['sourcePath'] = source
                path.write_text(json.dumps(catalog))
                with self.assertRaisesRegex(ValueError, 'invalid skill'):
                    validator.validate(self.root)

    def test_catalog_rejects_missing_duplicate_or_unknown_skills(self):
        path = self.root / 'site/catalog.json'
        original = path.read_text()
        for mutation in ('missing', 'duplicate', 'unknown'):
            with self.subTest(mutation=mutation):
                catalog = json.loads(original)
                if mutation == 'missing':
                    catalog['skills'].pop()
                elif mutation == 'duplicate':
                    catalog['skills'].append(catalog['skills'][0])
                else:
                    catalog['skills'][0]['id'] = 'unknown'
                path.write_text(json.dumps(catalog))
                with self.assertRaisesRegex(ValueError, 'every skill exactly once'):
                    validator.validate(self.root)

    def test_catalog_version_matches_skill_metadata(self):
        path = self.root / 'site/catalog.json'
        catalog = json.loads(path.read_text())
        catalog['skills'][0]['version'] = '99.0.0'
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, 'versions differ'):
            validator.validate(self.root)

    def test_missing_skill_version_is_rejected(self):
        path = self.root / 'skills/what-to-ask/SKILL.md'
        path.write_text(re.sub(r'(?m)^  version: .*\n', '', path.read_text()))
        with self.assertRaisesRegex(ValueError, 'invalid skill version'):
            validator.validate(self.root)

    def test_changed_provenance_requires_checksum_update(self):
        note = self.root / "skills/what-to-ask/tests/sources/dev-prompts-method.md"
        note.write_text(note.read_text() + "\nChanged\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            validator.validate(self.root)

    def test_private_content_is_rejected(self):
        private = self.root / "private.txt"
        private.write_text("/" + "Users" + "/example-user/private-file")
        with self.assertRaisesRegex(ValueError, "private path"):
            validator.validate(self.root)

    def test_malformed_eval_trajectory_is_rejected(self):
        path = self.root / "skills/delegate-code/tests/eval.yaml"
        path.write_text(path.read_text().replace("      trajectory:\n        in_order:", "      trajectory:\n        in_sequence:", 1))
        with self.assertRaisesRegex(ValueError, "invalid trajectory"):
            validator.validate(self.root)

    def test_standalone_skill_requires_its_license(self):
        (self.root / "skills/what-to-ask/LICENSE").unlink()
        with self.assertRaisesRegex(ValueError, "include a license"):
            validator.validate(self.root)

    def test_long_skill_is_rejected(self):
        path = self.root / "skills/what-to-ask/SKILL.md"
        path.write_text(path.read_text() + "filler\n" * 200)
        with self.assertRaisesRegex(ValueError, "what-to-ask: SKILL.md exceeds the 200 line limit"):
            validator.validate(self.root)

    def test_missing_or_escaping_referenced_file_is_rejected(self):
        path = self.root / "skills/second-opinion/SKILL.md"
        original = path.read_text()
        (self.root / "skills/second-opinion/references/guide").write_text("prefix of a spaced name")
        for reference in ("references/missing.md", "scripts/missing.py", "references/../../what-to-ask/SKILL.md", "references/..", "references/guide v2.md"):
            with self.subTest(reference=reference):
                path.write_text(original + f"\nSee `{reference}`.\n")
                with self.assertRaisesRegex(ValueError, "missing or outside the skill"):
                    validator.validate(self.root)

    def test_mcp_config_in_skill_is_rejected(self):
        (self.root / "skills/what-to-ask/tests/.mcp.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, r"\.mcp\.json is not allowed"):
            validator.validate(self.root)

    def test_cli_skill_requires_error_recovery_table(self):
        path = self.root / "skills/codex-image/SKILL.md"
        original = path.read_text()
        two_columns = "## Error Recovery\n\n| Failure | Recovery |\n| --- | --- |\n| a | b |\n| c | d |\n\n"
        one_row = "## Error Recovery\n\n| Failure | Cause | Recovery |\n| --- | --- | --- |\n| a | b | c |\n\n"
        blank_cells = "## Error Recovery\n\n| Failure | Cause | Recovery |\n| --- | --- | --- |\n| | | |\n| | | |\n\n"
        split_table = "## Error Recovery\n\n| Failure | Cause | Recovery |\n| --- | --- | --- |\n\n| a | b | c |\n\n| d | e | f |\n\n"
        for replacement in ("", two_columns, one_row, blank_cells, split_table):
            with self.subTest(replacement=replacement):
                path.write_text(re.sub(r"(?s)## Error Recovery.*?(?=## )", replacement, original))
                with self.assertRaisesRegex(ValueError, "codex-image: CLI skill needs an Error Recovery table"):
                    validator.validate(self.root)


if __name__ == "__main__":
    unittest.main()
