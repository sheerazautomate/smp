"""Static checks that keep the GitHub Actions workflows loadable by GitHub.

A workflow whose YAML parses can still be rejected by GitHub at load time — for
example a context used where it does not exist (``runner`` inside
workflow-level ``env``). GitHub then reports "Invalid workflow file" and the run
fails in zero seconds with no jobs. These tests catch that class of mistake
before it is pushed.

The context rules below cover the mistakes that are easy to make here; they are
not a full re-implementation of GitHub's expression validator. Run
``actionlint`` for deeper checking when it is available locally.
"""

import re
import unittest
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Contexts that only exist once a job/step is running. None of them may be used
# in a workflow-level ``env`` block, where GitHub evaluates expressions early.
JOB_ONLY_CONTEXT = re.compile(
    r"\b(runner|job|steps|strategy|matrix|needs)\.",
)
# Contexts that do not exist inside a ``jobs.<id>`` definition at all.
NOT_IN_JOB = re.compile(r"\b(inputs|steps)\.")


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _load_yaml():
    try:
        import yaml  # noqa: PLC0415 - optional test dependency
    except ImportError:  # pragma: no cover - exercised only without PyYAML
        return None
    return yaml


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


class WorkflowFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = _workflow_files()

    def test_workflow_files_exist(self):
        self.assertTrue(self.files, "no workflow files found under .github/workflows")

    def test_workflows_parse_as_yaml_mappings(self):
        yaml = _load_yaml()
        if yaml is None:
            self.skipTest("PyYAML is not installed")
        for path in self.files:
            with self.subTest(path=path.name):
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(document, dict)
                self.assertIn("jobs", document)

    def test_no_job_only_context_in_workflow_or_job_env(self):
        """``runner.temp`` et al. are invalid in workflow- and job-level ``env:``.

        GitHub evaluates those blocks before a runner is assigned, so a value
        like ``${{ runner.temp }}`` there makes the whole workflow file invalid
        and the run fails instantly with no jobs. Step-level ``env``/``with``
        may use the runner context, so only top-level and job-level blocks are
        checked.
        """
        for path in self.files:
            with self.subTest(path=path.name):
                lines = path.read_text(encoding="utf-8").splitlines()
                for index, line in enumerate(lines):
                    if line.strip() != "env:" or _indent(line) not in (0, 4):
                        continue
                    env_indent = _indent(line)
                    for body in lines[index + 1 :]:
                        stripped = body.strip()
                        if not stripped:
                            continue
                        # A new step or a shallower key ends the env block.
                        if _indent(body) <= env_indent or stripped.startswith("- "):
                            break
                        match = JOB_ONLY_CONTEXT.search(body)
                        if match:
                            self.fail(
                                f"{path.name}:{index + 1}: '{match.group(0)}' cannot be used "
                                f"in a workflow- or job-level env value: {stripped}"
                            )

    def test_no_inputs_context_outside_trigger_expressions(self):
        """``inputs`` is only available for workflows with a dispatch trigger."""
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            if "inputs." not in text:
                continue
            with self.subTest(path=path.name):
                self.assertRegex(
                    text,
                    r"(?m)^on:",
                    f"{path.name} uses inputs.* but has no 'on:' block",
                )
                self.assertRegex(
                    text,
                    r"(?m)^\s+workflow_dispatch:",
                    f"{path.name} uses inputs.* without a workflow_dispatch trigger",
                )

    def test_publish_steps_only_run_when_the_trigger_allows_it(self):
        """Committing back to the repository must be gated on the publish flag."""
        path = WORKFLOW_DIR / "smp-extract.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: write", text)
        self.assertIn("concurrency:", text)
        for name in ("Publish the bundle into data/exports", "Commit and push the export"):
            with self.subTest(step=name):
                block = text.split(f"- name: {name}", 1)
                self.assertEqual(len(block), 2, f"step {name!r} is missing")
                following = block[1].split("- name:", 1)[0]
                self.assertIn("env.PUBLISH == 'true'", following)

    def test_credentials_come_from_secrets_and_are_not_echoed(self):
        path = WORKFLOW_DIR / "smp-extract.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("SMP_USERNAME: ${{ secrets.SMP_USERNAME }}", text)
        self.assertIn("SMP_PASSWORD: ${{ secrets.SMP_PASSWORD }}", text)
        # Printing the variable *name* in an error message is fine; printing its
        # value (or dumping the whole environment) is not.
        leaks = re.compile(
            r"(\$\{?SMP_(PASSWORD|USERNAME)\}?|secrets\.SMP_(PASSWORD|USERNAME))"
        )
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("echo", "printf", "set -x")):
                self.assertIsNone(leaks.search(stripped), f"credentials echoed: {stripped}")
        self.assertNotIn("env |", text)
        self.assertNotIn("printenv", text)


if __name__ == "__main__":
    unittest.main()
