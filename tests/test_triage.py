from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from importlib import import_module  # noqa: E402


triage = import_module("02b_triage_candidates")


class PatchParsingTests(unittest.TestCase):
    def test_parses_new_and_existing_files(self) -> None:
        patch = """\
diff --git a/tests/new.rs b/tests/new.rs
new file mode 100644
--- /dev/null
+++ b/tests/new.rs
@@ -0,0 +1,2 @@
+#[test]
+fn catches_bug() {}
diff --git a/fixtures/existing.py b/fixtures/existing.py
--- a/fixtures/existing.py
+++ b/fixtures/existing.py
@@ -1 +1 @@
-old
+new
"""
        files = triage.parse_patch(patch)
        self.assertEqual([item.path for item in files], ["tests/new.rs", "fixtures/existing.py"])
        self.assertTrue(files[0].new_file)
        self.assertFalse(files[1].new_file)
        self.assertEqual(triage.added_test_functions(files), ["catches_bug"])

    def test_detects_test_registration_in_gold(self) -> None:
        files = [
            triage.PatchFile(
                path="src/rules/example/mod.rs",
                added_lines=["#[test_case(Rule::Example, Path::new(\"case.py\"))]"],
            )
        ]
        self.assertTrue(triage.contains_test_registration(files))


class CommandSuggestionTests(unittest.TestCase):
    def test_suggests_focused_server_test(self) -> None:
        files = [
            triage.PatchFile(
                path="crates/ruff_server/tests/e2e/workspace.rs",
                added_lines=["#[test]", "fn nested_workspace() {}"],
            )
        ]
        commands = triage.suggest_commands(
            "language-server",
            files,
            ["nested_workspace"],
        )
        self.assertEqual(
            commands,
            ["cargo test -p ruff_server --test e2e nested_workspace"],
        )

    def test_suggests_linter_module_filter(self) -> None:
        files = [
            triage.PatchFile(
                path=(
                    "crates/ruff_linter/resources/test/fixtures/"
                    "pydocstyle/existing.py"
                )
            )
        ]
        commands = triage.suggest_commands("linter", files, [])
        self.assertEqual(commands, ["cargo test -p ruff_linter pydocstyle"])

    def test_covers_multiple_touched_test_groups(self) -> None:
        files = [
            triage.PatchFile(path="crates/ruff/tests/cli/lint.rs"),
            triage.PatchFile(
                path=(
                    "crates/ruff_linter/resources/test/fixtures/"
                    "flake8_pyi/PYI061.py"
                )
            ),
        ]
        self.assertEqual(
            triage.suggest_commands("linter", files, []),
            [
                "cargo test -p ruff --test cli lint::",
                "cargo test -p ruff_linter flake8_pyi",
            ],
        )

    def test_direct_ruff_test_uses_its_own_target(self) -> None:
        files = [
            triage.PatchFile(
                path="crates/ruff/tests/show_settings.rs",
                added_lines=[
                    "#[test]",
                    "fn display_settings_from_nested_directory() {}",
                ],
            ),
            triage.PatchFile(
                path=(
                    "crates/ruff/tests/cli/snapshots/"
                    "cli__lint__example.snap"
                )
            ),
        ]
        commands = triage.suggest_commands(
            "configuration",
            files,
            ["display_settings_from_nested_directory"],
        )
        self.assertIn(
            (
                "cargo test -p ruff --test show_settings "
                "display_settings_from_nested_directory"
            ),
            commands,
        )


if __name__ == "__main__":
    unittest.main()
