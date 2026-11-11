"""Tests for the cache-aware dependency installer used by the workflows."""

import tempfile
import unittest
from pathlib import Path

from scripts.setup_workflow_env import (
    Plan,
    browser_cache_dir,
    build_plan,
    chromium_present,
    installed_browsers,
)

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


class BrowserCacheTests(unittest.TestCase):
    def test_missing_directory_has_no_browsers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(installed_browsers(Path(temp_dir) / "nope"), [])
            self.assertFalse(chromium_present(Path(temp_dir) / "nope"))

    def test_playwright_build_directories_are_recognised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("chromium-1181", "chromium_headless_shell-1181", "ffmpeg-1011"):
                (root / name).mkdir()
            (root / "not-a-browser").mkdir()
            (root / "stray-file").write_text("x")

            self.assertEqual(
                installed_browsers(root),
                ["chromium-1181", "chromium_headless_shell-1181"],
            )
            self.assertTrue(chromium_present(root))

    def test_headless_shell_alone_counts_as_chromium(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "chromium_headless_shell-1181").mkdir()
            self.assertTrue(chromium_present(root))

    def test_firefox_does_not_satisfy_the_chromium_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "firefox-1466").mkdir()
            self.assertFalse(chromium_present(root))

    def test_browser_cache_dir_honours_the_playwright_override(self):
        import os

        previous = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        try:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/example-browsers"
            self.assertEqual(browser_cache_dir(), Path("/tmp/example-browsers"))
        finally:
            if previous is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous


class PlanTests(unittest.TestCase):
    def test_cache_hit_skips_the_chromium_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "chromium-1181").mkdir()
            plan = build_plan(
                REQUIREMENTS, skip_browser=False, dry_run=True, cache_dir=root
            )
            self.assertTrue(plan.browser_cached)
            self.assertNotIn("playwright install chromium", plan.steps)
            self.assertIn("verify chromium launch", plan.steps)

    def test_cache_miss_plans_the_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_plan(
                REQUIREMENTS, skip_browser=False, dry_run=True, cache_dir=Path(temp_dir)
            )
            self.assertFalse(plan.browser_cached)
            self.assertIn("playwright install chromium", plan.steps)

    def test_skip_browser_never_plans_a_browser_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_plan(
                REQUIREMENTS, skip_browser=True, dry_run=True, cache_dir=Path(temp_dir)
            )
            self.assertFalse(any("playwright" in step for step in plan.steps))
            self.assertFalse(any("chromium" in step for step in plan.steps))

    def test_plan_always_installs_the_requirements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_plan(
                REQUIREMENTS, skip_browser=True, dry_run=True, cache_dir=Path(temp_dir)
            )
            self.assertTrue(
                any(step.startswith("pip install -r") for step in plan.steps)
            )

    def test_describe_reports_the_decisions(self):
        plan = Plan(
            requirements=REQUIREMENTS,
            browser_cached=True,
            pip_cached=True,
            skip_browser=False,
            dry_run=True,
            steps=["pip install -r requirements.txt"],
        )
        description = plan.describe()
        self.assertIn("chromium cached     : True", description)
        self.assertIn("install browser     : False", description)

    def test_dry_run_never_executes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            import contextlib
            import io

            from scripts.setup_workflow_env import main

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(
                    ["--dry-run", "--skip-browser", "--requirements", str(REQUIREMENTS)]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("[dry-run]", buffer.getvalue())

    def test_missing_requirements_file_is_reported(self):
        import contextlib
        import io

        from scripts.setup_workflow_env import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            exit_code = main(["--dry-run", "--requirements", "/tmp/does-not-exist.txt"])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
