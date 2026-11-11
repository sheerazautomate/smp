#!/usr/bin/env python3
"""Install the workflow's dependencies, reusing caches where they are valid.

The GitHub Actions workflow caches two things between runs:

* ``~/.cache/pip`` — wheels, keyed on ``requirements.txt``.
* ``~/.cache/ms-playwright`` — the Chromium build, keyed on the Playwright
  version.

Both are restored by the workflow before this script runs. This script decides
what still needs doing, does only that, and records timings so the next run's
optimisation is based on measurements rather than guesswork.

Run it locally with ``--dry-run`` to see the plan without changing anything.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def browser_cache_dir() -> Path:
    """Where Playwright stores downloaded browsers.

    Playwright uses ``PLAYWRIGHT_BROWSERS_PATH`` when set, otherwise
    ``~/.cache/ms-playwright`` on Linux and ``~/Library/Caches/ms-playwright``
    on macOS.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def installed_browsers(cache_dir: Path) -> list[str]:
    """Browser build directories present in the cache, e.g. ``chromium-1181``."""
    if not cache_dir.is_dir():
        return []
    names = []
    for child in cache_dir.iterdir():
        if not child.is_dir():
            continue
        if re.match(r"^(chromium|chromium_headless_shell|firefox|webkit)-\d+$", child.name):
            names.append(child.name)
    return sorted(names)


def chromium_present(cache_dir: Path) -> bool:
    return any(name.startswith("chromium") for name in installed_browsers(cache_dir))


def playwright_version() -> str:
    """Version of the installed Playwright package (cache key material)."""
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:
        return "unknown"


def pip_cache_dir() -> Path:
    override = os.environ.get("PIP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "pip"
    return Path.home() / ".cache" / "pip"


def _run(command: list[str], dry_run: bool) -> int:
    printable = " ".join(command)
    if dry_run:
        print(f"[dry-run] {printable}")
        return 0
    print(f"+ {printable}", flush=True)
    return subprocess.call(command)


def _set_output(name: str, value: str) -> None:
    """Expose a step output, and an env var for later steps."""
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


@dataclass
class Plan:
    """What this run intends to do, decided before anything is executed."""

    requirements: Path
    browser_cached: bool
    pip_cached: bool
    skip_browser: bool
    dry_run: bool
    steps: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return "\n".join(
            [
                f"requirements        : {self.requirements}",
                f"pip cache directory : {pip_cache_dir()}",
                f"pip cache restored  : {self.pip_cached}",
                f"browser cache dir   : {browser_cache_dir()}",
                f"chromium cached     : {self.browser_cached}",
                f"install browser     : {not (self.browser_cached or self.skip_browser)}",
                f"plan                : {', '.join(self.steps) or 'nothing to do'}",
            ]
        )


def build_plan(
    requirements: Path,
    *,
    skip_browser: bool,
    dry_run: bool,
    cache_dir: Path | None = None,
) -> Plan:
    directory = cache_dir if cache_dir is not None else browser_cache_dir()
    pip_dir = pip_cache_dir()
    return Plan(
        requirements=requirements,
        browser_cached=chromium_present(directory),
        pip_cached=pip_dir.is_dir() and any(pip_dir.iterdir()),
        skip_browser=skip_browser,
        dry_run=dry_run,
        steps=[
            "pip install --upgrade pip",
            f"pip install -r {requirements.name}",
            *(
                []
                if skip_browser or chromium_present(directory)
                else ["playwright install chromium"]
            ),
            *([] if skip_browser else ["verify chromium launch"]),
        ],
    )


def install_python_deps(plan: Plan) -> float:
    started = time.monotonic()
    status = _run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], plan.dry_run)
    if status != 0:
        raise SystemExit(f"pip upgrade failed with exit code {status}")
    status = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(plan.requirements),
        ],
        plan.dry_run,
    )
    if status != 0:
        raise SystemExit(f"dependency installation failed with exit code {status}")
    return time.monotonic() - started


def install_browser(plan: Plan) -> float:
    """Install Chromium only when the restored cache does not already have it.

    ``playwright install`` is idempotent and skips an up-to-date build, but it
    still costs seconds of startup on every run. Skipping it outright when a
    ``chromium-*`` build is present keeps a cache hit fast; ``verify_browser``
    below is what proves the cached build actually works, so a stale or partial
    cache fails immediately instead of mid-extraction.
    """
    started = time.monotonic()
    if plan.browser_cached:
        print("Chromium build already present in the cache; skipping download.")
        return time.monotonic() - started
    status = _run([sys.executable, "-m", "playwright", "install", "chromium"], plan.dry_run)
    if status != 0:
        raise SystemExit(f"Chromium installation failed with exit code {status}")
    return time.monotonic() - started


def verify_browser(plan: Plan) -> bool:
    """Launch Chromium once so a broken cache fails here, not mid-extraction."""
    if plan.dry_run:
        print("[dry-run] verify chromium launch")
        return True
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    browser = p.chromium.launch()\n"
        "    page = browser.new_page()\n"
        "    page.set_content('<p>ok</p>')\n"
        "    print('chromium launch ok:', page.inner_text('p'))\n"
        "    browser.close()\n"
    )
    status = subprocess.call([sys.executable, "-c", script])
    return status == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup-workflow-env",
        description=(
            "Install Python dependencies and Chromium for the extraction "
            "workflow, reusing the caches the workflow restored."
        ),
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=REPO / "requirements.txt",
        help="Requirements file to install (default: requirements.txt).",
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Do not install or verify Chromium (used by the lint/test workflow).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without installing anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.requirements.is_file():
        print(f"::error::requirements file not found: {args.requirements}", file=sys.stderr)
        return 2

    plan = build_plan(
        args.requirements, skip_browser=args.skip_browser, dry_run=args.dry_run
    )
    print(plan.describe())
    print()

    timings: list[tuple[str, float]] = []
    timings.append(("python deps", install_python_deps(plan)))
    if not plan.skip_browser:
        timings.append(("chromium", install_browser(plan)))
        if not verify_browser(plan):
            print("::error::Chromium could not be launched after installation.", file=sys.stderr)
            print(
                "::warning::If this was a cache hit, the browser cache is stale. "
                "Delete the 'playwright-*' cache entries and re-run.",
                file=sys.stderr,
            )
            return 1
        _set_output("CHROMIUM_READY", "1")

    total = sum(seconds for _, seconds in timings)
    print("\nsetup timings")
    print("=" * 40)
    for name, seconds in timings:
        print(f"{name:>12}: {seconds:6.1f}s")
    print(f"{'total':>12}: {total:6.1f}s")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and not args.dry_run:
        cached = "yes" if plan.browser_cached else "no"
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Dependency setup\n\n")
            handle.write(f"- Playwright {playwright_version()}\n")
            handle.write(f"- Chromium restored from cache: {cached}\n")
            handle.write(f"- Setup took {total:.1f}s\n")
            for name, seconds in timings:
                handle.write(f"  - {name}: {seconds:.1f}s\n")

    if shutil.which("python") is None:  # pragma: no cover - defensive
        print("::warning::No python executable on PATH.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
