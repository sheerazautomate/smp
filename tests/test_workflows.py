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

    def test_partial_exports_fail_the_job(self):
        """A short capture must never leave a green run behind."""
        text = (WORKFLOW_DIR / "smp-extract.yml").read_text(encoding="utf-8")
        self.assertIn("EXTRACT_INCOMPLETE=1", text)
        self.assertIn("Fail the run if any school was incomplete", text)
        self.assertIn("exit 1", text.split("Fail the run if any school was incomplete")[1])

    def test_chromium_and_pip_are_cached(self):
        """Re-downloading Chromium every run was the main avoidable cost."""
        text = (WORKFLOW_DIR / "smp-extract.yml").read_text(encoding="utf-8")
        self.assertIn("name: Cache Playwright browsers", text)
        self.assertIn("~/.cache/ms-playwright", text)
        self.assertIn("name: Cache pip downloads and wheels", text)
        # The browser cache key must move when the Playwright pin changes.
        browser_block = text.split("name: Cache Playwright browsers", 1)[1]
        browser_block = browser_block.split("name:", 1)[0]
        self.assertIn("hashFiles('requirements.txt')", browser_block)
        self.assertIn("steps.browser-cache.outputs.cache-hit", text)

    def test_workflow_actions_use_current_major_versions(self):
        """Node 20 actions were deprecated; old pins emit warnings on every run."""
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("actions/checkout@v4", text)
                self.assertNotIn("actions/setup-python@v5", text)
                self.assertNotIn("actions/upload-artifact@v4", text)
                self.assertNotIn("actions/cache@v4", text)

    def test_dependency_install_goes_through_the_cache_aware_script(self):
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            # The Sheets sync job installs two pure-Python packages and never
            # launches a browser, so it has no browser cache to populate.
            if "sync_sheets.py" in text:
                with self.subTest(path=path.name):
                    self.assertNotIn("playwright", text)
                    self.assertNotIn("pip install -r requirements.txt", text)
                    # The job runs the whole suite, which imports openpyxl for
                    # the exporter tests. Omitting it failed a real run before
                    # the sheet was ever touched. Check an *install* line, not
                    # any mention: a comment naming the package must not satisfy
                    # this (that mistake made the guard pass while the run broke).
                    executed = "\n".join(
                        line
                        for line in text.splitlines()
                        if not line.strip().startswith("#")
                    )
                    self.assertIn('"openpyxl', executed)
                continue
            with self.subTest(path=path.name):
                self.assertIn("scripts/setup_workflow_env.py", text)
                # A bare pip install would bypass the cache logic.
                self.assertNotIn("pip install -r requirements.txt", text)

    def test_unit_tests_run_without_downloading_a_browser(self):
        text = (WORKFLOW_DIR / "checks.yml").read_text(encoding="utf-8")
        self.assertIn("--skip-browser", text)

    def test_install_steps_never_run_playwright_through_sudo(self):
        """sudo resolves its own `python`, which lacks the installed packages.

        This broke a real run: `sudo python -m playwright install-deps chromium`
        failed with exit code 1 before extraction because sudo's PATH is not the
        setup-python interpreter.
        """
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for line in text.splitlines():
                    stripped = line.strip()
                    # Only executable lines; prose in comments may mention sudo.
                    if stripped.startswith("#") or "playwright" not in stripped:
                        continue
                    self.assertNotIn(
                        "sudo",
                        stripped,
                        f"{path.name}: playwright must not run under sudo: {stripped}",
                    )

    def test_every_browser_install_path_installs_the_os_libraries(self):
        """A cache miss must still end up with a launchable Chromium."""
        text = (WORKFLOW_DIR / "smp-extract.yml").read_text(encoding="utf-8")
        # Either the script downloads a browser, or the workflow installs deps.
        self.assertTrue(
            "--with-deps" in text or "install-deps" in text,
            "no step installs Chromium's system libraries",
        )
        self.assertIn("--with-deps chromium", text)

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

    # ------------------------------------------------------------------ #
    # The Google Sheets sync workflow
    # ------------------------------------------------------------------ #

    def _sync_text(self) -> str:
        return (WORKFLOW_DIR / "sync-sheets.yml").read_text(encoding="utf-8")

    def test_sync_workflow_exists_and_runs_after_extraction(self):
        text = self._sync_text()
        self.assertIn('workflows: ["SMP table extract"]', text)
        self.assertIn("workflow_run", text)
        # workflow_run ignores github.ref_name for the upstream branch.
        self.assertIn("github.event.workflow_run.head_branch", text)

    def test_sync_only_runs_after_a_successful_extraction(self):
        text = self._sync_text()
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)

    def test_sync_reads_the_sheet_configuration_from_secrets(self):
        text = self._sync_text()
        self.assertIn("SMP_SHEET_ID: ${{ secrets.SMP_SHEET_ID }}", text)
        self.assertIn(
            "GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}",
            text,
        )

    def test_sync_never_echoes_the_service_account_key(self):
        text = self._sync_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("echo", "printf", "set -x")):
                self.assertNotIn(
                    "$GOOGLE_SERVICE_ACCOUNT_JSON}",
                    stripped.replace("${GOOGLE_SERVICE_ACCOUNT_JSON}", ""),
                    f"service-account key echoed: {stripped}",
                )
                self.assertNotIn('cat "$KEY_PATH"', stripped)
        self.assertNotIn("env |", text)
        self.assertNotIn("printenv", text)

    def test_sync_key_file_lives_outside_the_workspace_and_is_removed(self):
        text = self._sync_text()
        self.assertIn("$RUNNER_TEMP/smp-sheets-key.json", text)
        self.assertIn("always()", text)
        self.assertIn("rm -f", text)

    def test_sync_workflow_never_touches_the_raw_tabs(self):
        text = self._sync_text()
        self.assertNotIn("RAW Milk", text.replace("RAW Milk and Raw Biscuits are never written", ""))
        self.assertNotIn("Raw Biscuits", text.replace("RAW Milk and Raw Biscuits are never written", ""))


if __name__ == "__main__":
    unittest.main()
