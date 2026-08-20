"""Browser test: the Phase 1 vertical slice.

Runs a real Chromium against a real Next.js build and a real API. What it
actually proves, beyond "the page renders":

* the cookie-based session survives a client-side navigation
* the access token never appears in localStorage or sessionStorage
* the candidate sees their own assessment and the integrity disclosure
* signing out clears the session and the protected route bounces to login

Phase 4+ will add the harder cases from TESTING.md — fullscreen exit,
visibility change, forced reload mid-exam, offline queue flush. Those need an
exam runner, which does not exist yet.

Usage:
    WEB_BASE_URL=http://localhost:3000 SEED_PASSWORD=... python test_login_flow.py
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import expect, sync_playwright


def _chromium_path() -> str | None:
    """Resolve the pinned Chromium.

    PLAYWRIGHT_BROWSERS_PATH installs are versioned (chromium-1194/...), so the
    path is discovered rather than hard-coded; returning None lets Playwright
    fall back to its own resolution on a normal developer machine.
    """
    import glob

    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


WEB = os.getenv("WEB_BASE_URL", "http://localhost:3000")
DOMAIN = f"{os.getenv('SEED_ORG_SLUG', 'demo-university')}.example.edu"
PASSWORD = os.getenv("SEED_PASSWORD", "")
CANDIDATE = f"aisha.rahman@{DOMAIN}"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    if not PASSWORD:
        print("SEED_PASSWORD is not set.", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium_path())
        context = browser.new_context()
        page = context.new_page()

        # Track HTTP failures by status and URL rather than by console text.
        # "Failed to load resource" tells you nothing; a status and a path tell
        # you what to fix.
        http_errors: list[tuple[int, str]] = []
        page.on(
            "response",
            lambda r: http_errors.append((r.status, r.url)) if r.status >= 400 else None,
        )

        # ---------------------------------------------------------- login
        page.goto(f"{WEB}/login", wait_until="networkidle")
        check("login page renders", page.get_by_role("heading", name="Sentinel").is_visible())

        page.fill("#email", CANDIDATE)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/exams", timeout=15_000)

        # -------------------------------------------------- session state
        expect(page.get_by_role("heading", name="Your assessments")).to_be_visible()
        check("redirected to /exams after login", "/exams" in page.url)

        # The exam list is fetched after the route renders. Reading
        # page.content() straight after the navigation races that fetch — it
        # passes or fails depending on machine speed, which is worse than a
        # failing test because it erodes trust in the whole suite. Wait for the
        # element instead.
        exam_row = page.get_by_role("heading", name="Programming Fundamentals — Final")
        try:
            expect(exam_row).to_be_visible(timeout=10_000)
            listed = True
        except AssertionError:
            listed = False
        check("assigned exam is listed", listed, "the seeded exam did not render")

        body = page.inner_text("main")
        check("candidate name is shown", "Aisha Rahman" in body, "display_name missing")
        check(
            "integrity monitoring is disclosed before the exam",
            "Integrity monitoring is enabled" in body,
        )
        check(
            "no live flag feed is shown to the candidate (D7)",
            "flagged" not in body.lower() and "risk score" not in body.lower(),
            "candidate-facing copy exposes flags or a risk score",
        )

        # ------------------------------------------------ token handling
        local = page.evaluate("() => JSON.stringify(Object.entries(localStorage))")
        session_storage = page.evaluate("() => JSON.stringify(Object.entries(sessionStorage))")
        check(
            "no credentials in localStorage",
            "token" not in local.lower() and "sentinel_access" not in local,
            f"localStorage={local[:120]}",
        )
        check(
            "no credentials in sessionStorage",
            "token" not in session_storage.lower(),
            f"sessionStorage={session_storage[:120]}",
        )

        cookies = {c["name"]: c for c in context.cookies()}
        check("access cookie exists", "sentinel_access" in cookies)
        check(
            "access cookie is httpOnly",
            cookies.get("sentinel_access", {}).get("httpOnly") is True,
            "an XSS bug would otherwise be account takeover",
        )
        check(
            "csrf cookie is readable by the app",
            cookies.get("sentinel_csrf", {}).get("httpOnly") is False,
        )

        # The access token must not be reachable from page script.
        reachable = page.evaluate("() => document.cookie.includes('sentinel_access')")
        check("access token is not reachable from document.cookie", reachable is False)

        # ------------------------------------------------------- reload
        page.reload(wait_until="networkidle")
        expect(page.get_by_role("heading", name="Your assessments")).to_be_visible()
        check("session survives a full page reload", "/exams" in page.url)

        # ------------------------------------------------------- logout
        page.get_by_role("button", name="Sign out").click()
        page.wait_for_url("**/login", timeout=15_000)
        check("sign out returns to login", "/login" in page.url)

        after = {c["name"] for c in context.cookies()}
        check("access cookie cleared on logout", "sentinel_access" not in after)

        # Protected route must bounce an anonymous visitor.
        page.goto(f"{WEB}/exams", wait_until="networkidle")
        page.wait_for_url("**/login", timeout=15_000)
        check("protected route redirects when signed out", "/login" in page.url)

        # One HTTP error is expected and is not a defect: 401 on /auth/me for a
        # signed-out visitor, which is how the session store discovers
        # anonymity. Everything else fails the run. Naming the exception
        # explicitly, rather than suppressing errors wholesale, keeps a new
        # failure visible.
        def expected(status: int, url: str) -> bool:
            return status == 401 and url.endswith("/api/v1/auth/me")

        unexpected = [e for e in http_errors if not expected(*e)]
        check(
            "no unexpected HTTP errors during the flow",
            not unexpected,
            "; ".join(f"{s} {u}" for s, u in unexpected[:3]),
        )

        page.screenshot(path="/tmp/sentinel-login.png")
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all browser checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
