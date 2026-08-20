"""Browser test: the Phase 2 exam runner, in a real Chromium.

What this proves that the API tests cannot:

* the candidate can actually answer a question with a mouse and a keyboard
* autosave reaches the server without the candidate doing anything
* **a forced reload mid-exam loses nothing** — the Phase 2 exit criterion that
  is really about the browser, not the API. `test_assessment_flow.py` proves the
  server can restore a session; only this proves the client asks it to.
* the countdown is driven by the server's clock, and moving the browser's clock
  does not move it
* the answer key is not in the DOM

Usage:
    WEB_BASE_URL=http://localhost:3000 API_BASE_URL=http://localhost:8000 \
    SEED_PASSWORD=... python test_exam_runner.py
"""

from __future__ import annotations

import glob
import os
import re
import sys

from playwright.sync_api import Page, expect, sync_playwright


def _chromium_path() -> str | None:
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
CANDIDATE = os.getenv("RUNNER_CANDIDATE", f"luis.ferreira@{DOMAIN}")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def _sign_in(page: Page) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("#email", CANDIDATE)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/exams", timeout=15_000)


def _remaining(page: Page) -> int:
    text = page.locator("p.tabular-nums").first.inner_text().strip()
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def main() -> int:
    if not PASSWORD:
        print("SEED_PASSWORD is not set.", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_chromium_path())
        context = browser.new_context()
        page = context.new_page()

        http_errors: list[tuple[int, str]] = []
        page.on(
            "response",
            lambda r: http_errors.append((r.status, r.url)) if r.status >= 400 else None,
        )

        _sign_in(page)

        # --------------------------------------------------------- start
        start = page.get_by_role("button", name="Start or continue").first
        expect(start).to_be_enabled(timeout=10_000)
        start.click()
        page.wait_for_url("**/take", timeout=15_000)

        expect(page.get_by_text("Time remaining")).to_be_visible(timeout=15_000)
        questions = page.locator("ol > li")
        expect(questions.first).to_be_visible(timeout=15_000)
        count = questions.count()
        check("the paper renders with questions", count > 0, f"count={count}")

        # ------------------------------------------------- the answer key
        html = page.content()
        leaks = [k for k in ('"correct"', '"accepted"', '"tolerance"') if k in html]
        check("no answer key in the DOM", not leaks, f"found {leaks}")

        # ------------------------------------------------------- the clock
        first_reading = _remaining(page)
        check("countdown is running", first_reading > 0, f"read {first_reading}")

        # Move the browser's wall clock two hours forward. The countdown is
        # anchored to performance.now() and to the server's remaining_seconds,
        # so it must not move. A client that trusted Date.now() would jump.
        page.evaluate(
            "() => { const real = Date.now; Date.now = () => real() + 7200_000; }"
        )
        page.wait_for_timeout(1500)
        after_skew = _remaining(page)
        check(
            "the countdown ignores the browser clock",
            first_reading - after_skew < 10,
            f"{first_reading} -> {after_skew} after a two-hour clock skew",
        )

        # ------------------------------------------------------- autosave
        radio = page.locator("input[type=radio]").first
        text_input = page.locator("input[type=text]").first
        answered_label = ""

        if radio.count() > 0:
            radio.check()
            answered_label = "radio"
        elif text_input.count() > 0:
            text_input.fill("Dijkstra")
            answered_label = "text"
        check("an answer could be entered", answered_label != "")

        expect(page.get_by_text("All answers saved")).to_be_visible(timeout=10_000)
        check("autosave reports success without any explicit save action", True)

        progress = page.locator("text=/\\d+ of \\d+ answered/").first.inner_text()
        check(
            "progress counter reflects the answer",
            re.match(r"^[1-9]", progress) is not None,
            progress,
        )

        # ------------------------------------------- forced reload recovery
        # The Phase 2 exit criterion, from the browser's side. A hard reload
        # throws away every scrap of client state; everything below has to come
        # back from the server.
        before_prompts = [q.inner_text() for q in questions.all()]
        page.reload(wait_until="networkidle")
        expect(page.get_by_text("Time remaining")).to_be_visible(timeout=15_000)

        recovered = page.locator("ol > li")
        expect(recovered.first).to_be_visible(timeout=15_000)
        check(
            "the same paper comes back after a forced reload",
            recovered.count() == count,
            f"{count} -> {recovered.count()} questions",
        )

        after_prompts = [q.inner_text() for q in recovered.all()]
        check(
            "questions come back in the same order",
            [p.split("\n")[0] for p in before_prompts]
            == [p.split("\n")[0] for p in after_prompts],
            "the paper was regenerated rather than restored",
        )

        if answered_label == "radio":
            restored = page.locator("input[type=radio]:checked").count() > 0
        else:
            restored = "Dijkstra" in page.locator("input[type=text]").first.input_value()
        check("the saved answer survives the reload", restored, "the answer was lost")

        check(
            "no answer key in the DOM after recovery either",
            not any(k in page.content() for k in ('"correct"', '"accepted"')),
        )

        # -------------------------------------------------------- submit
        page.get_by_role("button", name="Submit exam").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=5_000)
        check(
            "the confirmation names the cost of unanswered questions",
            "score zero" in dialog.inner_text() or "All" in dialog.inner_text(),
            dialog.inner_text()[:120],
        )
        dialog.get_by_role("button", name="Submit").click()

        expect(page.get_by_role("heading", name="Submitted")).to_be_visible(timeout=15_000)
        check("submission confirms", True)

        # A submitted session must be readable but not editable.
        page.reload(wait_until="networkidle")
        expect(page.get_by_role("heading", name="Submitted")).to_be_visible(timeout=15_000)
        check(
            "a submitted session reopens read-only",
            page.locator("input[type=radio]").count() == 0
            and page.locator("input[type=text]").count() == 0,
            "editable inputs are still on the page after submission",
        )

        def expected(status: int, url: str) -> bool:
            # 401 on /auth/me: how the session store discovers anonymity.
            # 409 on POST /sessions: reopening the page after submitting. The
            # attempts are used up, the server correctly refuses to start a new
            # one, and the client falls back to fetching the finished session.
            # Both are designed paths, not defects — naming them explicitly
            # keeps any *other* error visible.
            if status == 401 and url.endswith("/api/v1/auth/me"):
                return True
            return status == 409 and url.endswith("/api/v1/sessions")

        unexpected = [e for e in http_errors if not expected(*e)]
        check(
            "no unexpected HTTP errors during the flow",
            not unexpected,
            "; ".join(f"{s} {u}" for s, u in unexpected[:3]),
        )

        page.screenshot(path="/tmp/sentinel-exam-submitted.png", full_page=True)
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all exam-runner browser checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
