from __future__ import annotations

import getpass
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from .models import ExtractedTable, SchoolResult
from .table_parser import normalize_table

DEFAULT_BASE_URL = "https://smp26.pesrp.edu.pk"
DEFAULT_CODES = (
    "32230155",
    "32230183",
    "32230566",
    "32230576",
    "32230763",
    "32230764",
)

TAB_SELECTOR = (
    '[role="tab"], [data-bs-toggle="tab"], [data-bs-toggle="pill"], '
    '[data-toggle="tab"], [data-toggle="pill"]'
)
INTERACTIVE_SELECTOR = 'a, button, [role="button"]'

TABLE_SNAPSHOT_JS = r"""() => {
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const cellText = cell => {
    const clone = cell.cloneNode(true);
    clone.querySelectorAll('table').forEach(nested => nested.remove());
    return clean(clone.innerText || clone.textContent || '');
  };
  const sectionHeading = table => {
    const headingSelector = 'h1,h2,h3,h4,h5,h6,legend,.card-title,.panel-title,.section-title';
    for (let node = table; node && node !== document.body; node = node.parentElement) {
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (sibling.matches(headingSelector)) return clean(sibling.innerText || sibling.textContent);
        const found = sibling.querySelector(headingSelector);
        if (found) return clean(found.innerText || found.textContent);
      }
    }
    return '';
  };
  return Array.from(document.querySelectorAll('table')).map((table, index) => {
    const rows = Array.from(table.querySelectorAll('tr'))
      .filter(row => row.closest('table') === table)
      .map(row => ({
        in_thead: !!(row.closest('thead') && row.closest('thead').closest('table') === table),
        cells: Array.from(row.children)
          .filter(cell => cell.tagName === 'TH' || cell.tagName === 'TD')
          .map(cell => ({
            text: cellText(cell),
            tag: cell.tagName,
            scope: cell.getAttribute('scope') || '',
            role: cell.getAttribute('role') || '',
            colspan: cell.colSpan || 1,
            rowspan: cell.rowSpan || 1
          }))
      }));
    const caption = table.caption ? clean(table.caption.innerText || table.caption.textContent) : '';
    const label = table.getAttribute('aria-label') || table.getAttribute('summary') || '';
    return {
      index,
      id: table.id || table.getAttribute('data-table-id') || table.getAttribute('data-dt-id') || '',
      title: caption || clean(label) || sectionHeading(table),
      rows
    };
  });
}"""

TAB_DESCRIPTORS_JS = r"""() => Array.from(document.querySelectorAll(
  '[role="tab"], [data-bs-toggle="tab"], [data-bs-toggle="pill"], [data-toggle="tab"], [data-toggle="pill"]'
)).map((element, index) => ({
  index,
  text: (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim(),
  href: element.getAttribute('href') || '',
  role: element.getAttribute('role') || '',
  toggle: element.getAttribute('data-bs-toggle') || element.getAttribute('data-toggle') || '',
  selected: element.getAttribute('aria-selected') === 'true',
  active: element.classList.contains('active') || !!(element.parentElement && element.parentElement.classList.contains('active'))
}))"""

COLLAPSE_CANDIDATES_JS = r"""() => {
  const selector = 'a,button,[role="button"]';
  const elements = Array.from(document.querySelectorAll(selector));
  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const candidates = [];
  elements.forEach((element, index) => {
    const toggle = element.getAttribute('data-bs-toggle') || element.getAttribute('data-toggle') || '';
    const expanded = element.getAttribute('aria-expanded');
    const controls = element.getAttribute('aria-controls') || '';
    const href = element.getAttribute('href') || '';
    const targetId = controls || (href.startsWith('#') ? href.slice(1) : '');
    const target = targetId ? document.getElementById(targetId) : null;
    const targetIsCollapse = !!(target && (
      target.classList.contains('collapse') || target.classList.contains('accordion-collapse')
    ));
    const targetIsOpen = !!(target && (
      target.classList.contains('show') || target.classList.contains('in')
    ));
    const targetLooksCollapsed = !!(target && (
      (targetIsCollapse && !targetIsOpen) ||
      target.hidden || target.getAttribute('aria-hidden') === 'true'
    ));
    const collapseControl = toggle === 'collapse' ||
      element.classList.contains('accordion-button') ||
      (expanded === 'false' && targetLooksCollapsed);
    const collapsed = expanded === 'false' || element.classList.contains('collapsed') || targetLooksCollapsed;
    const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true' ||
      element.classList.contains('disabled') || !!(element.closest('li.disabled'));
    const localHref = !href || href === '#' || href.startsWith('#') || href.toLowerCase().startsWith('javascript:');
    if (!collapseControl || !collapsed || disabled || !localHref || !visible(element)) return;
    const label = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    const signature = [element.id, toggle, controls, href, label].join('|');
    candidates.push({index, label, signature});
  });
  return candidates;
}"""

PAGINATION_CANDIDATES_JS = r"""() => {
  const selector = 'a,button,[role="button"]';
  const elements = Array.from(document.querySelectorAll(selector));
  const containerSelector = '.pagination,[class*="pagination" i],[class*="pager" i],[class*="paginate" i],nav[aria-label*="page" i],[role="navigation"][aria-label*="page" i]';
  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const candidates = [];
  elements.forEach((element, index) => {
    const container = element.closest(containerSelector);
    if (!container || !visible(element)) return;
    const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    const aria = element.getAttribute('aria-label') || '';
    const title = element.getAttribute('title') || '';
    const classes = String(element.className || '');
    const rel = element.getAttribute('rel') || '';
    const isNext = /\bnext\b/i.test(`${text} ${aria} ${title}`) ||
      /^(›|»|→|>>|››|»»)$/.test(text) ||
      /(^|\s)next(\s|$)/i.test(classes) || /(^|\s)next(\s|$)/i.test(rel);
    const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true' ||
      element.classList.contains('disabled') || !!element.closest('li.disabled,[aria-disabled="true"]');
    if (!isNext || disabled) return;
    candidates.push({
      index,
      text,
      aria,
      href: element.getAttribute('href') || '',
      signature: [container.id, container.className, container.innerText, aria, title, text].join('|').slice(0, 500)
    });
  });
  return candidates;
}"""

PAGE_DIGEST_JS = r"""() => Array.from(document.querySelectorAll('table'))
  .map(table => (table.innerText || table.textContent || '').replace(/\s+/g, ' ').trim())
  .join('\u0000')"""


class ExtractionError(RuntimeError):
    pass


class RateLimitedError(ExtractionError):
    pass


class AuthenticationError(ExtractionError):
    pass


def detail_url(base_url: str, emis_code: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", f"school-detail/{emis_code}")


def _is_detail_path(url: str, code: str) -> bool:
    path = urlsplit(url).path.rstrip("/")
    return path.endswith(f"/school-detail/{code}")


def _visible_password(page: Any) -> bool:
    try:
        return page.locator('input[type="password"]:visible').count() > 0
    except Exception:
        return False


def _find_username_field(page: Any, password_field: Any | None = None) -> Any | None:
    roots = []
    if password_field is not None:
        try:
            form = password_field.locator("xpath=ancestor::form[1]")
            if form.count() > 0:
                roots.append(form)
        except Exception:
            pass
    roots.append(page)

    preferred = re.compile(r"user|email|login|account", re.I)
    excluded_types = {"password", "hidden", "submit", "button", "checkbox", "radio", "search"}
    for root in roots:
        inputs = root.locator("input:visible")
        candidates: list[tuple[int, Any]] = []
        for index in range(inputs.count()):
            locator = inputs.nth(index)
            input_type = (locator.get_attribute("type") or "text").lower()
            if input_type in excluded_types:
                continue
            attributes = " ".join(
                [input_type]
                + [
                    locator.get_attribute(name) or ""
                    for name in ("name", "id", "placeholder", "autocomplete", "aria-label")
                ]
            )
            if (locator.get_attribute("autocomplete") or "").lower() == "username":
                score = 0
            elif preferred.search(attributes):
                score = 1
            elif input_type == "email":
                score = 2
            else:
                score = 3
            candidates.append((score, locator))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
    return None


def _get_credentials(non_interactive: bool) -> tuple[str, str]:
    username = os.environ.get("SMP_USERNAME", "").strip()
    password = os.environ.get("SMP_PASSWORD", "")

    if not username:
        if non_interactive:
            raise AuthenticationError("SMP_USERNAME is required in non-interactive mode.")
        username = input("Portal username: ").strip()
    if not password:
        if non_interactive:
            raise AuthenticationError("SMP_PASSWORD is required in non-interactive mode.")
        password = getpass.getpass("Portal password (input is hidden): ")

    if not username or not password:
        raise AuthenticationError("Both username and password are required to sign in.")
    return username, password


def _settle(page: Any, short_wait_ms: int = 350) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=2500)
    except Exception:
        pass
    page.wait_for_timeout(short_wait_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=900)
    except Exception:
        pass


def _goto(page: Any, url: str, timeout_ms: int) -> Any:
    response = None
    for attempt in range(2):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            if attempt == 1:
                raise ExtractionError(f"Navigation failed ({type(exc).__name__}).") from exc
            time.sleep(1.5)
            continue

        status = response.status if response is not None else None
        if status == 429:
            raise RateLimitedError("Portal returned HTTP 429; stopping to avoid overloading it.")
        if status is not None and status >= 500 and attempt == 0:
            time.sleep(1.5)
            continue
        if status is not None and status >= 400:
            raise ExtractionError(f"Portal returned HTTP {status} for the requested page.")
        _settle(page)
        return response

    return response


def _submit_login(page: Any, non_interactive: bool) -> None:
    password_field = page.locator('input[type="password"]:visible').first
    username_field = _find_username_field(page, password_field)
    if username_field is None:
        raise AuthenticationError(
            "A login form was detected, but no visible username field could be identified."
        )

    username, password = _get_credentials(non_interactive)
    try:
        username_field.fill(username)
        password_field.fill(password)
    except Exception as exc:
        raise AuthenticationError("Could not fill the detected login form.") from exc

    submit = page.locator('button[type="submit"]:visible, input[type="submit"]:visible').first
    try:
        if submit.count() > 0:
            submit.click(timeout=5000)
        else:
            login_button = page.get_by_role("button", name=re.compile(r"log\s*in|sign\s*in|submit", re.I)).first
            if login_button.count() > 0 and login_button.is_visible():
                login_button.click(timeout=5000)
            else:
                password_field.press("Enter")
    except Exception as exc:
        raise AuthenticationError("Could not submit the detected login form.") from exc

    _settle(page, short_wait_ms=700)
    if _visible_password(page):
        raise AuthenticationError(
            "The portal still shows a password field after sign-in. Check the account details or portal response."
        )


def _ensure_authenticated(
    page: Any,
    base_url: str,
    first_code: str,
    timeout_ms: int,
    non_interactive: bool,
) -> None:
    target = detail_url(base_url, first_code)
    _goto(page, target, timeout_ms)

    if _visible_password(page):
        _submit_login(page, non_interactive)
        _goto(page, target, timeout_ms)
    elif not _is_detail_path(page.url, first_code):
        # Some portals redirect a protected detail URL to the landing page
        # before exposing the login form there.
        _goto(page, base_url, timeout_ms)
        if _visible_password(page):
            _submit_login(page, non_interactive)
            _goto(page, target, timeout_ms)

    if _visible_password(page):
        raise AuthenticationError("The portal is still requesting a sign-in.")
    if not _is_detail_path(page.url, first_code):
        raise AuthenticationError(
            "Could not reach the requested school detail page after sign-in."
        )


def _capture_tables(
    page: Any,
    result: SchoolResult,
    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable],
    context: str,
) -> None:
    snapshots = page.evaluate(TABLE_SNAPSHOT_JS)
    for snapshot in snapshots:
        table = normalize_table(snapshot, result.emis_code, page.url, context)
        key = (table.table_ref, table.title, tuple(table.headers))
        existing = table_map.get(key)
        if existing is None:
            table_map[key] = table
            result.tables = sorted(
                table_map.values(), key=lambda item: (item.table_index, item.title.casefold())
            )
            continue
        existing.add_snapshot(table.rows, page.url, context)
        if table.table_index < existing.table_index:
            existing.table_index = table.table_index
        result.tables = sorted(
            table_map.values(), key=lambda item: (item.table_index, item.title.casefold())
        )


def _tab_descriptors(page: Any, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    try:
        return page.evaluate(TAB_DESCRIPTORS_JS)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"Could not inspect page tabs ({type(exc).__name__}).")
        return []


def _same_school_route(page_url: str, href: str, code: str) -> bool:
    if not href or href == "#" or href.startswith("#") or href.lower().startswith("javascript:"):
        return True
    candidate = urlsplit(urljoin(page_url, href))
    current = urlsplit(page_url)
    return candidate.netloc == current.netloc and candidate.path.rstrip("/") == current.path.rstrip("/") and _is_detail_path(candidate.geturl(), code)


def _click_tab(
    page: Any,
    descriptor: dict[str, Any],
    detail_page_url: str,
    code: str,
    timeout_ms: int,
) -> tuple[bool, str]:
    label = " ".join(str(descriptor.get("text", "")).split()) or "unnamed tab"
    href = str(descriptor.get("href", ""))
    if not _same_school_route(page.url, href, code):
        return False, label

    try:
        locator = page.locator(TAB_SELECTOR).nth(int(descriptor.get("index", 0)))
        if not locator.is_visible():
            return False, label
        active_state = locator.evaluate(
            "el => el.getAttribute('aria-selected') === 'true' || "
            "el.classList.contains('active') || "
            "!!(el.parentElement && el.parentElement.classList.contains('active'))"
        )
        if not active_state:
            locator.click(timeout=min(timeout_ms, 6000))
            _settle(page, short_wait_ms=350)
        if not _is_detail_path(page.url, code):
            _goto(page, detail_page_url, timeout_ms)
            return False, label
        return True, label
    except Exception:
        return False, label


def _expand_collapsed_sections(
    page: Any,
    result: SchoolResult,
    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable],
    context: str,
    detail_page_url: str,
    max_interactions: int = 100,
    timeout_ms: int = 45000,
) -> None:
    blocked: set[str] = set()
    clicks = 0
    while clicks < max_interactions:
        try:
            candidates = page.evaluate(COLLAPSE_CANDIDATES_JS)
        except Exception as exc:
            result.warnings.append(
                f"Could not inspect expandable sections ({type(exc).__name__})."
            )
            return
        candidate = next(
            (item for item in candidates if item.get("signature") not in blocked), None
        )
        if candidate is None:
            break

        signature = str(candidate.get("signature", ""))
        before_url = page.url
        try:
            locator = page.locator(INTERACTIVE_SELECTOR).nth(int(candidate["index"]))
            locator.click(timeout=4500)
            _settle(page, short_wait_ms=300)
            clicks += 1
        except Exception as exc:
            result.warnings.append(
                f"Could not activate an expandable section ({type(exc).__name__})."
            )
            blocked.add(signature)
            continue

        if not _is_detail_path(page.url, result.emis_code):
            result.warnings.append("A collapsed-section control navigated away; returned to the school page.")
            _goto(page, detail_page_url, timeout_ms)
            break

        _capture_tables(page, result, table_map, f"{context}; expanded section")
        if page.url == before_url:
            # If the control remains collapsed, do not repeatedly click it.
            still_collapsed = page.evaluate(
                """signature => {
                  const all = Array.from(document.querySelectorAll('a,button,[role="button"]'));
                  return all.some(element => {
                    const toggle = element.getAttribute('data-bs-toggle') || element.getAttribute('data-toggle') || '';
                    const controls = element.getAttribute('aria-controls') || '';
                    const href = element.getAttribute('href') || '';
                    const label = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
                    const current = [element.id, toggle, controls, href, label].join('|');
                    return current === signature && (element.getAttribute('aria-expanded') === 'false' || element.classList.contains('collapsed'));
                  });
                }""",
                signature,
            )
            if still_collapsed:
                blocked.add(signature)

    if clicks >= max_interactions:
        try:
            remaining = [
                item
                for item in page.evaluate(COLLAPSE_CANDIDATES_JS)
                if item.get("signature") not in blocked
            ]
        except Exception:
            remaining = []
        if remaining:
            result.warnings.append("Stopped expanding sections at the configured safety limit.")


def _pagination_candidates(
    page: Any, code: str, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
    try:
        candidates = page.evaluate(PAGINATION_CANDIDATES_JS)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"Could not inspect pagination controls ({type(exc).__name__}).")
        return []
    return [
        candidate
        for candidate in candidates
        if _same_school_route(page.url, str(candidate.get("href", "")), code)
    ]


def _paginate_current_view(
    page: Any,
    result: SchoolResult,
    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable],
    context: str,
    detail_page_url: str,
    max_pages: int,
    timeout_ms: int,
) -> None:
    blocked: set[str] = set()
    clicks = 0

    while True:
        _capture_tables(page, result, table_map, f"{context}; pagination state {clicks + 1}")
        candidates = _pagination_candidates(page, result.emis_code, result.warnings)
        candidate = next(
            (item for item in candidates if item.get("signature") not in blocked), None
        )
        if candidate is None:
            break
        if clicks >= max_pages:
            result.warnings.append(
                f"Stopped pagination after the configured limit of {max_pages} page advances."
            )
            break

        before_url = page.url
        try:
            before_digest = page.evaluate(PAGE_DIGEST_JS)
            locator = page.locator(INTERACTIVE_SELECTOR).nth(int(candidate["index"]))
            locator.click(timeout=min(timeout_ms, 7000))
            clicks += 1
            _settle(page, short_wait_ms=450)
        except Exception as exc:
            result.warnings.append(
                f"Could not activate a pagination control ({type(exc).__name__})."
            )
            blocked.add(str(candidate.get("signature", "")))
            continue

        if not _is_detail_path(page.url, result.emis_code):
            result.warnings.append("A pagination control left the requested school page; returned to it.")
            _goto(page, detail_page_url, timeout_ms)
            break

        after_digest = page.evaluate(PAGE_DIGEST_JS)
        if page.url == before_url and before_digest == after_digest:
            # Give asynchronous UI updates a second chance before deciding the
            # candidate is a no-op. Avoids an unbounded click loop on disabled
            # or decorative controls.
            page.wait_for_timeout(700)
            after_digest = page.evaluate(PAGE_DIGEST_JS)
            if page.url == before_url and before_digest == after_digest:
                result.warnings.append(
                    "A pagination control did not change the rendered tables; stopped that control."
                )
                blocked.add(str(candidate.get("signature", "")))
            else:
                blocked.clear()
        else:
            blocked.clear()


def _extract_school(
    page: Any,
    result: SchoolResult,
    max_pages: int,
    timeout_ms: int,
) -> None:
    detail_page_url = result.url
    _goto(page, detail_page_url, timeout_ms)
    if _visible_password(page):
        raise AuthenticationError("The portal requested a new sign-in while processing a school.")
    if not _is_detail_path(page.url, result.emis_code):
        raise ExtractionError("The portal did not remain on the requested school detail page.")

    try:
        result.page_title = page.title()
    except Exception:
        result.page_title = ""

    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable] = {}
    _capture_tables(page, result, table_map, "initial page state")

    # Expand any default-view sections first; this also catches a tab control
    # that only appears after its parent section is opened.
    _expand_collapsed_sections(
        page, result, table_map, "default view", detail_page_url, timeout_ms=timeout_ms
    )
    tabs = _tab_descriptors(page, result.warnings)
    processable_tabs = [
        tab
        for tab in tabs
        if (not tab.get("href") or str(tab.get("href")).startswith("#"))
        and (tab.get("role") == "tab" or tab.get("toggle") in {"tab", "pill"})
    ]

    if not processable_tabs:
        _paginate_current_view(
            page,
            result,
            table_map,
            "default view",
            detail_page_url,
            max_pages,
            timeout_ms,
        )
    else:
        for tab in processable_tabs:
            clicked, label = _click_tab(
                page, tab, detail_page_url, result.emis_code, timeout_ms
            )
            context = f"tab: {label}" if clicked else f"tab (not activated): {label}"
            if not clicked:
                result.warnings.append(f"Could not activate or verify tab: {label}.")
            _capture_tables(page, result, table_map, context)
            _expand_collapsed_sections(
                page, result, table_map, context, detail_page_url, timeout_ms=timeout_ms
            )
            _paginate_current_view(
                page,
                result,
                table_map,
                context,
                detail_page_url,
                max_pages,
                timeout_ms,
            )

    result.tables = sorted(
        table_map.values(), key=lambda item: (item.table_index, item.title.casefold())
    )
    result.status = "ok" if result.tables else "no_tables"
    if not result.tables:
        result.warnings.append("No HTML tables were found in the rendered school detail page.")


def extract_schools(
    base_url: str,
    emis_codes: list[str],
    *,
    delay_seconds: float = 1.5,
    headless: bool = False,
    timeout_seconds: int = 45,
    max_pages: int = 100,
    non_interactive: bool = False,
) -> list[SchoolResult]:
    """Log in once and collect tables for each requested EMIS detail page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExtractionError(
            "Playwright is not installed. Run `pip install -r requirements.txt` and "
            "`python -m playwright install chromium`."
        ) from exc

    timeout_ms = timeout_seconds * 1000
    outcomes: list[SchoolResult] = []
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
        except Exception as exc:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            raise ExtractionError(
                "Could not start Chromium. Run `python -m playwright install chromium` "
                f"and check that its system dependencies are installed ({type(exc).__name__})."
            ) from exc

        page.set_default_timeout(min(timeout_ms, 10000))
        try:
            _ensure_authenticated(
                page,
                base_url,
                emis_codes[0],
                timeout_ms,
                non_interactive,
            )

            stop_reason = ""
            for index, code in enumerate(emis_codes):
                url = detail_url(base_url, code)
                result = SchoolResult(emis_code=code, url=url)
                if stop_reason:
                    result.status = "skipped"
                    result.error = stop_reason
                    outcomes.append(result)
                    continue

                if index and delay_seconds > 0:
                    time.sleep(delay_seconds)

                try:
                    _extract_school(page, result, max_pages, timeout_ms)
                except RateLimitedError as exc:
                    result.status = "error"
                    result.error = str(exc)
                    stop_reason = "Stopped after the portal rate-limited the session."
                except AuthenticationError as exc:
                    result.status = "error"
                    result.error = str(exc)
                    stop_reason = "Stopped because the authenticated session was no longer available."
                except Exception as exc:
                    result.status = "error"
                    message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    result.error = f"{type(exc).__name__}: {message[:300]}"
                outcomes.append(result)
        finally:
            context.close()
            browser.close()

    return outcomes
