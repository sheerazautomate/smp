from __future__ import annotations

import getpass
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from .models import ExtractedTable, SchoolResult
from .pagination import PagerState, pager_states
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

PAGER_INFO_JS = r"""() => {
  // Report *structure* only; text parsing and page math live in Python so they
  // can be unit-tested without a browser. One record per <table>.
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const tables = Array.from(document.querySelectorAll('table'));

  return tables.map((table, tableIndex) => {
    const id = table.id || '';
    let infoNode = null;
    let pagerEl = null;
    let lengthEl = null;

    // Canonical DataTables association: <id>_info / <id>_paginate / <id>_length.
    if (id) {
      const byId = suffix => {
        const node = document.getElementById(id + suffix);
        return node && visible(node) ? node : null;
      };
      infoNode = byId('_info') || document.getElementById(id + '_info');
      pagerEl = byId('_paginate');
      lengthEl = byId('_length');
    }
    // Fallback: aria-describedby, the documented way to link a table to its info.
    if (!infoNode) {
      const described = (table.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      for (const candidate of described) {
        const node = document.getElementById(candidate);
        // Reject an info node that belongs to a different table.
        const owner = /^(.*)_info$/.exec(candidate);
        if (node && (!owner || owner[1] === id)) { infoNode = node; break; }
      }
    }

    let lengthValue = null;
    let lengthHasAll = false;
    if (lengthEl) {
      const select = lengthEl.querySelector('select');
      if (select) {
        lengthValue = select.value;
        lengthHasAll = Array.from(select.options).some(option => String(option.value) === '-1');
      }
    }

    // Page markers: data-dt-idx on the anchors, li.active for the current page.
    const numericIdxs = [];
    let activeIdx = null;
    if (pagerEl) {
      pagerEl.querySelectorAll('a').forEach(anchor => {
        const raw = anchor.getAttribute('data-dt-idx');
        const text = clean(anchor.innerText || anchor.textContent);
        let idx = null;
        if (raw !== null && raw !== '' && !Number.isNaN(Number(raw))) idx = Number(raw);
        else if (/^\d+$/.test(text)) idx = Number(text) - 1;
        if (idx === null) return;
        numericIdxs.push(idx);
        const li = anchor.closest('li');
        const active = anchor.getAttribute('aria-current') === 'page' ||
          !!(li && li.classList.contains('active'));
        if (active) activeIdx = idx;
      });
    }

    return {
      tableIndex,
      tableId: id,
      infoText: infoNode ? clean(infoNode.innerText || infoNode.textContent) : '',
      hasPager: !!pagerEl,
      hasLength: !!lengthEl,
      lengthValue,
      lengthHasAll,
      numericIdxs,
      activeIdx,
    };
  });
}"""

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
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (rect.width > 0 && rect.height > 0) return true;
      // A table inside a hidden tab panel still matters once its tab is opened,
      // but its current rows are stale; report whether it is rendered now.
      return false;
    };
    const caption = table.caption ? clean(table.caption.innerText || table.caption.textContent) : '';
    const label = table.getAttribute('aria-label') || table.getAttribute('summary') || '';
    return {
      index,
      id: table.id || table.getAttribute('data-table-id') || table.getAttribute('data-dt-id') || '',
      classes: String(table.className || ''),
      visible: visible(table),
      title: caption || clean(label) || sectionHeading(table),
      rows
    };
  });
}"""

# Buttons that swap content can be styled as plain Bootstrap tabs, as
# data-bs-toggle controls, *or* as bare buttons driven by a custom script (the
# portal's Milk/Biscuit pair are plain <button id="tab-milk"> elements). The DOM
# cannot tell those apart from any other button, so the caller also tries
# buttons that sit next to a peer and reveal new tables when clicked.
TAB_SELECTOR_JS = (
    '[role="tab"], [data-bs-toggle="tab"], [data-bs-toggle="pill"], '
    '[data-toggle="tab"], [data-toggle="pill"]'
)

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

#: Candidate content-switching buttons that are *not* declared tabs.
TAB_LIKE_CANDIDATES_JS = r"""() => {
  // Content switchers styled as plain buttons (e.g. id="tab-milk"). Only
  // considered when marked active, or grouped with a sibling control, so
  // ordinary action buttons are not clicked blindly.
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const declared = Array.from(document.querySelectorAll(
    '[role="tab"], [data-bs-toggle="tab"], [data-bs-toggle="pill"], [data-toggle="tab"], [data-toggle="pill"]'
  ));
  const all = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"]'));
  const out = [];
  all.forEach((element, index) => {
    if (declared.includes(element)) return;
    if (!visible(element)) return;
    const label = clean(element.innerText || element.textContent);
    const id = element.id || '';
    const marker = `${label} ${id}`;
    const looksLikeTab = /(^|[-_\s])tab([-_\s]|$)/i.test(marker) ||
      /(^|\s)(nav-link|btn-group|segmented)(\s|$)/i.test(String(element.className || ''));
    const explicitlyActive = element.classList.contains('active') ||
      element.getAttribute('aria-selected') === 'true' ||
      element.getAttribute('aria-pressed') === 'true';
    const parent = element.parentElement;
    const siblingControls = parent
      ? Array.from(parent.children).filter(node => node !== element && node.matches('button, a')).length
      : 0;
    // Content switchers come in groups; a lone active button is something else
    // (a styled action button), so it is not treated as a view.
    if (siblingControls === 0) return;
    if (!looksLikeTab && !explicitlyActive) return;
    out.push({
      index,
      text: label,
      id: id,
      signature: [id, label].join('|'),
      active: explicitlyActive,
    });
  });
  return out;
}"""

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
    // A control sitting next to a peer control in a button group is a content
    // switcher (tab/pill), not a collapse toggle. Without this the Milk/Biscuit
    // tabs were clicked here, clicking the already-open one timed out, and the
    // real tabs then looked "blocked".
    const parent = element.parentElement;
    const inControlGroup = !!(parent && Array.from(parent.children)
      .filter(node => node !== element && node.matches('button, a')).length > 0);
    if (inControlGroup) return;
    if (!collapseControl || !collapsed || disabled || !localHref || !visible(element)) return;
    const label = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    const signature = [element.id, toggle, controls, href, label].join('|');
    candidates.push({index, label, signature});
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
        # Hidden tab panels keep whatever was rendered last; capturing them would
        # attribute stale rows to a table that has not been visited yet. Each view
        # is captured explicitly while it is visible instead.
        if snapshot.get("visible") is False:
            continue
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
        # Match on the tab's label/href instead of a positional index: the DOM
        # can re-render between inspection and click, shifting indices.
        locator = page.locator(TAB_SELECTOR).filter(
            has_text=label if label != "unnamed tab" else None
        ).first
        if locator.count() == 0 or not locator.is_visible():
            return False, label
        active_state = locator.evaluate(
            "el => el.getAttribute('aria-selected') === 'true' || "
            "el.classList.contains('active') || "
            "!!(el.parentElement && el.parentElement.classList.contains('active'))"
        )
        if not active_state:
            before_digest = page.evaluate(PAGE_DIGEST_JS)
            locator.click(timeout=min(timeout_ms, 6000))
            _settle(page, short_wait_ms=350)
            if page.evaluate(PAGE_DIGEST_JS) == before_digest:
                return False, label
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
            handle = page.evaluate_handle(
                """signature => {
                  const all = Array.from(document.querySelectorAll('a,button,[role="button"]'));
                  return all.find(element => {
                    const toggle = element.getAttribute('data-bs-toggle') || element.getAttribute('data-toggle') || '';
                    const controls = element.getAttribute('aria-controls') || '';
                    const href = element.getAttribute('href') || '';
                    const label = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
                    return [element.id, toggle, controls, href, label].join('|') === signature;
                  }) || null;
                }""",
                signature,
            )
            element = handle.as_element()
            if element is None:
                blocked.add(signature)
                continue
            element.click(timeout=4500)
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


def _pager_records(page: Any, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    """Raw per-table pager structure, straight from the rendered DOM."""
    try:
        return page.evaluate(PAGER_INFO_JS)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"Could not inspect pagination controls ({type(exc).__name__}).")
        return []


def _current_pagers(page: Any, warnings: list[str] | None = None) -> dict[int, PagerState]:
    return pager_states(_pager_records(page, warnings))


def _set_page_length_all(page: Any, table_id: str) -> bool:
    """Ask DataTables to render every row at once via its length selector.

    ``select_option`` dispatches the native ``change`` event, which is what the
    DataTables/jQuery listener reacts to.
    """
    if not table_id:
        return False
    try:
        locator = page.locator(f"#{table_id}_length select").first
        if locator.count() == 0:
            return False
        locator.select_option("-1", timeout=5000)
        _settle(page, short_wait_ms=450)
        return True
    except Exception:
        return False


def _view_candidates(page: Any, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    """Content-switching controls, declared tabs first, then tab-like buttons."""
    views: list[dict[str, Any]] = []
    for tab in _tab_descriptors(page, warnings):
        href = str(tab.get("href") or "")
        is_local = not href or href.startswith("#")
        declared = tab.get("role") == "tab" or tab.get("toggle") in {"tab", "pill"}
        if is_local and declared:
            label = " ".join(str(tab.get("text") or "").split()) or "unnamed tab"
            views.append(
                {
                    "kind": "tab",
                    "descriptor": tab,
                    "label": label,
                    "signature": str(tab.get("signature") or label),
                    "declared": True,
                    "active": bool(tab.get("active") or tab.get("selected")),
                }
            )
    try:
        extras = page.evaluate(TAB_LIKE_CANDIDATES_JS)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"Could not inspect tab-like buttons ({type(exc).__name__}).")
        extras = []
    seen = {view["label"] for view in views}
    for extra in extras or []:
        label = " ".join(str(extra.get("text") or "").split()) or str(extra.get("id") or "")
        if not label or label in seen:
            continue
        seen.add(label)
        views.append(
            {
                "kind": "button",
                "descriptor": extra,
                "label": label,
                "signature": str(extra.get("signature") or label),
                "declared": False,
                "active": bool(extra.get("active")),
            }
        )
    return views


def _activate_view(
    page: Any,
    view: dict[str, Any],
    detail_page_url: str,
    code: str,
    timeout_ms: int,
) -> tuple[str, bool]:
    """Click a view's control; return ``(label, activated)``."""
    label = str(view.get("label") or "unnamed tab")
    if view.get("kind") == "tab":
        clicked, resolved = _click_tab(
            page, view["descriptor"], detail_page_url, code, timeout_ms
        )
        return resolved, clicked

    signature = str(view.get("signature") or label)
    try:
        handle = page.evaluate_handle(
            """signature => {
              const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const all = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"]'));
              return all.find(element => [element.id || '', clean(element.innerText || element.textContent)].join('|') === signature) || null;
            }""",
            signature,
        )
        element = handle.as_element()
        if element is None:
            return label, False
        before_digest = page.evaluate(PAGE_DIGEST_JS)
        element.click(timeout=min(timeout_ms, 6000))
        _settle(page, short_wait_ms=400)
        after_digest = page.evaluate(PAGE_DIGEST_JS)
        if after_digest == before_digest and not view.get("active"):
            # Clicking must change what is rendered. A no-op click would make the
            # next capture attribute this panel's rows to the other table.
            page.wait_for_timeout(600)
            if page.evaluate(PAGE_DIGEST_JS) == before_digest:
                return label, False
    except Exception:
        return label, False
    if not _is_detail_path(page.url, code):
        _goto(page, detail_page_url, timeout_ms)
        return label, False
    return label, True


def _load_view(
    page: Any,
    result: SchoolResult,
    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable],
    context: str,
    detail_page_url: str,
    max_pages: int,
    timeout_ms: int,
) -> None:
    """Capture the current view once, then complete every table in it."""
    _capture_tables(page, result, table_map, context)
    _paginate_current_view(
        page, result, table_map, context, detail_page_url, max_pages, timeout_ms
    )
    _tag_view_pagers(page, result, context)


def _tag_view_pagers(page: Any, result: SchoolResult, context: str) -> None:
    """Attach each table's own totals and remember which view they came from.

    ``TABLE_SNAPSHOT_JS`` and ``PAGER_INFO_JS`` both index tables in whole-document
    order, so matching on that index is exact — no guessing from list positions,
    which mis-associated totals when a tab hid one of the tables.
    """
    pagers = _current_pagers(page, result.warnings)
    by_index = {table.table_index: table for table in result.tables}
    for index, pager in pagers.items():
        table = by_index.get(index)
        if table is None:
            continue
        table.rows_expected = pager.total_rows
        table.pages_expected = pager.page_count
        table.pager_view = context
        table.pager_kind = "numbered-pages" if pager.has_pager else "single-page"
        # Count rendered page states for this view, not every capture: the
        # "show all" snapshot is one state even though it follows page 1.
        page_states = [
            c
            for c in table.capture_contexts
            if c.startswith(context) and ("; page " in c or c == context)
        ]
        table.pages_captured = max(1, len(page_states))


def _report_incomplete(result: SchoolResult) -> None:
    """Warn (and later downgrade status) for every short capture."""
    for table in result.incomplete_tables:
        result.warnings.append(
            f"Incomplete table capture: {table.title} "
            f"({table.row_count} of {table.rows_expected} rows)."
        )


def _paginate_current_view(
    page: Any,
    result: SchoolResult,
    table_map: dict[tuple[str, str, tuple[str, ...]], ExtractedTable],
    context: str,
    detail_page_url: str,
    max_pages: int,
    timeout_ms: int,
) -> None:
    """Capture every page of every paginated table in the current view.

    Each table is driven through its own pager (``<id>_length`` /
    ``<id>_paginate`` / ``<id>_info``), so one table's pager never advances
    another table's rows. The portal's "Showing 1 to 5 of 18 entries" line is
    the source of truth for how many rows the export should contain.
    """
    _capture_tables(page, result, table_map, f"{context}; page 1")
    pagers = _current_pagers(page, result.warnings)

    for index in sorted(pagers):
        pager = pagers[index]
        if not pager.is_paginated:
            continue
        label = pager.table_id or f"table #{index + 1}"
        pages_expected = pager.page_count or 1

        # Fast path: "All" in the length selector renders the table in one go.
        # Selecting it can raise if the select is replaced by a re-render, which
        # is why the caller falls back to walking pages.
        if pager.can_show_all and _set_page_length_all(page, pager.table_id):
            _capture_tables(page, result, table_map, f"{context}; {label}; show all")
            refreshed = _current_pagers(page, result.warnings).get(index)
            if refreshed is not None:
                pager = refreshed
                pagers[index] = refreshed
            if not pager.is_paginated:
                continue

        blocked: set[str] = set()
        clicks = 0
        visited = {pager.current_page if pager.current_page is not None else 0}
        while clicks < max_pages:
            candidates = _page_step_candidates(
                page, pager.table_id, result.emis_code, visited
            )
            candidate = next(
                (item for item in candidates if item.get("signature") not in blocked),
                None,
            )
            if candidate is None:
                break

            before_url = page.url
            try:
                before_digest = page.evaluate(PAGE_DIGEST_JS)
                # Click an element handle resolved by identity. Re-querying by
                # positional index races with any DOM re-render in between.
                handle = page.evaluate_handle(
                    """signature => {
                      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                      const all = Array.from(document.querySelectorAll('a,button,[role="button"]'));
                      return all.find(element => {
                        const text = clean(element.innerText || element.textContent);
                        return [element.getAttribute('aria-controls') || '', text].join('|') === signature;
                      }) || null;
                    }""",
                    candidate["signature"],
                )
                element = handle.as_element()
                if element is None:
                    blocked.add(str(candidate.get("signature", "")))
                    continue
                element.click(timeout=min(timeout_ms, 7000))
                clicks += 1
                _settle(page, short_wait_ms=450)
            except Exception as exc:
                result.warnings.append(
                    f"Could not activate a pagination control ({type(exc).__name__})."
                )
                blocked.add(str(candidate.get("signature", "")))
                continue

            if not _is_detail_path(page.url, result.emis_code):
                result.warnings.append(
                    "A pagination control left the requested school page; returned to it."
                )
                _goto(page, detail_page_url, timeout_ms)
                break

            after_digest = page.evaluate(PAGE_DIGEST_JS)
            if page.url == before_url and before_digest == after_digest:
                # One more chance for an asynchronous re-render before giving up.
                page.wait_for_timeout(700)
                after_digest = page.evaluate(PAGE_DIGEST_JS)
                if page.url == before_url and before_digest == after_digest:
                    result.warnings.append(
                        "A pagination control did not change the rendered tables; stopped that control."
                    )
                    blocked.add(str(candidate.get("signature", "")))
                    continue

            page_index = candidate.get("pageIndex")
            visited.add(int(page_index) if page_index is not None else len(visited))
            _capture_tables(
                page,
                result,
                table_map,
                f"{context}; {label}; page {len(visited)} of {pages_expected}",
            )

        if clicks >= max_pages:
            result.warnings.append(
                f"Stopped pagination after the configured limit of {max_pages} page advances."
            )


def _page_step_candidates(
    page: Any, table_id: str, code: str, visited: set[int]
) -> list[dict[str, Any]]:
    """Next unvisited page button inside one table's pager."""
    try:
        raw = page.evaluate(
            """([tableId, visited]) => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const pager = tableId ? document.getElementById(tableId + '_paginate') : null;
              const scope = pager || document.querySelector('[class*="pagination" i]');
              if (!scope) return [];
              const seen = new Set(visited);
              const out = [];
              scope.querySelectorAll('a,button').forEach(element => {
                const text = clean(element.innerText || element.textContent);
                const dtIdx = element.getAttribute('data-dt-idx');
                const aria = element.getAttribute('aria-label') || '';
                const rel = element.getAttribute('rel') || '';
                const classes = String(element.className || '');
                const li = element.closest('li');
                const disabled = element.disabled ||
                  element.getAttribute('aria-disabled') === 'true' ||
                  classes.split(/\s+/).includes('disabled') ||
                  !!(li && (li.classList.contains('disabled') || li.getAttribute('aria-disabled') === 'true'));
                if (disabled) return;
                const isNext = /\bnext\b/i.test(text + ' ' + aria) ||
                  /^(>|>>|›|»|→)$/.test(text) ||
                  /(^|\s)next(\s|$)/i.test(classes) || /(^|\s)next(\s|$)/i.test(rel);
                const isPrev = /\b(previous|prev)\b/i.test(text + ' ' + aria) ||
                  /^(<|<<|‹|«|←)$/.test(text) ||
                  /(^|\s)previous(\s|$)/i.test(classes);
                let pageIndex = null;
                if (dtIdx !== null && dtIdx !== '' && !Number.isNaN(Number(dtIdx))) pageIndex = Number(dtIdx);
                else if (/^\d+$/.test(text)) pageIndex = Number(text) - 1;
                const active = !!(li && li.classList.contains('active')) ||
                  element.getAttribute('aria-current') === 'page';
                if (active) return;
                if (pageIndex !== null && seen.has(pageIndex)) return;
                if (pageIndex === null && !isNext) return;
                if (isPrev && pageIndex === null) return;
                out.push({
                  pageIndex,
                  next: isNext,
                  signature: [element.getAttribute('aria-controls') || '', text].join('|'),
                });
              });
              out.sort((a, b) => {
                if (a.pageIndex !== null && b.pageIndex !== null) return a.pageIndex - b.pageIndex;
                if (a.pageIndex !== null) return -1;
                if (b.pageIndex !== null) return 1;
                return 0;
              });
              return out;
            }""",
            [table_id, sorted(visited)],
        )
    except Exception:
        return []
    return [
        item for item in raw
        if _same_school_route(page.url, item.get("href", ""), code)
    ]


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

    # Expand any default-view sections first; this also catches a control that
    # only appears after its parent section is opened.
    _expand_collapsed_sections(
        page, result, table_map, "default view", detail_page_url, timeout_ms=timeout_ms
    )

    # A *view* is one rendered state that can hide tables: the default view, or
    # one per tab. Each view is loaded completely when it is entered, so a tab is
    # never left half-captured merely because it was clicked last.
    views = _view_candidates(page, result.warnings)
    contexts: dict[str, dict[str, Any] | None] = {"default view": None}

    _load_view(
        page,
        result,
        table_map,
        "default view",
        detail_page_url,
        max_pages,
        timeout_ms,
    )

    for view in views:
        if view.get("active"):
            # Already on screen — the default view pass captured it. Clicking it
            # would be a no-op and would look like a failed activation.
            continue
        label, clicked = _activate_view(
            page, view, detail_page_url, result.emis_code, timeout_ms
        )
        if not clicked:
            result.warnings.append(f"Could not activate or verify tab: {label}.")
            continue
        context = f"tab: {label}"
        contexts[context] = view
        _capture_tables(page, result, table_map, context)
        _expand_collapsed_sections(
            page, result, table_map, context, detail_page_url, timeout_ms=timeout_ms
        )
        _load_view(
            page, result, table_map, context, detail_page_url, max_pages, timeout_ms
        )

    # Second pass: a view that still holds an incomplete table is revisited and
    # finished. This is what keeps a table behind a tab from being left at page 1.
    for _ in range(len(contexts)):
        incomplete_views = {
            table.pager_view
            for table in table_map.values()
            if table.pager_view and not table.is_complete
        }
        target = next(
            (context for context in incomplete_views if context in contexts), None
        )
        if target is None:
            break
        view = contexts.pop(target)
        if view is None:  # default view is not reachable by clicking a tab
            continue
        _activate_view(page, view, detail_page_url, result.emis_code, timeout_ms)
        _load_view(
            page, result, table_map, target, detail_page_url, max_pages, timeout_ms
        )

    result.tables = sorted(
        table_map.values(), key=lambda item: (item.table_index, item.title.casefold())
    )
    _report_incomplete(result)
    if not result.tables:
        result.status = "no_tables"
    elif result.incomplete_tables:
        result.status = "incomplete"
    else:
        result.status = "ok"
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
