"""Optional real-browser journey against an isolated, loopback-only app process."""

import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.browser, pytest.mark.loopback,
              pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 and install a Playwright browser")]
ROOT = Path(__file__).resolve().parents[1]


def test_keyboard_login_queue_cancel_comparison_review_and_logout(tmp_path):
    import json
    import requests
    from playwright.sync_api import sync_playwright, expect
    from shared.deployments import Deployment, DeploymentRegistry

    registry = DeploymentRegistry([Deployment("isolated", "ollama", "http://inference.invalid:11434", {"small": "tiny:3b"})])
    config = tmp_path / "registry.json"
    config.write_text(json.dumps(registry.to_dict()), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ, PYTHON_DOTENV_DISABLED="1", OTP_DEV_MODE="true",
        CARBON_DEPLOYMENTS_FILE=str(config), CARBON_QUEUE_DATABASE=str(tmp_path / "queue.sqlite3"),
        STREAMLIT_BROWSER_GATHER_USAGE_STATS="false", STREAMLIT_SERVER_FILE_WATCHER_TYPE="none")
    log = (tmp_path / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "workflow/dashboard/app.py",
        "--server.address", "127.0.0.1", "--server.port", str(port), "--server.headless", "true"],
        cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        for _ in range(60):
            if process.poll() is not None:
                pytest.fail("The isolated Streamlit server exited during startup")
            try:
                if requests.get(url + "/_stcore/health", timeout=.5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(.25)
        else:
            pytest.fail("The isolated Streamlit server did not become healthy")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel=os.getenv("PLAYWRIGHT_CHANNEL") or None,
                                                  args=["--disable-background-networking"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/")
                           else route.abort())
                page.goto(url)
                expect(page.get_by_role("heading", name=re.compile("Secure Login"))).to_be_visible()
                page.get_by_label("Email Address", exact=True).fill("browser-fixture@example.invalid")
                # Exercise a native keyboard activation path, including visible labels.
                send = page.get_by_role("button", name="Send Verification Code", exact=True)
                send.focus()
                page.keyboard.press("Enter")
                code_notice = page.get_by_text(re.compile("Your verification code is:"))
                expect(code_notice).to_be_visible()
                code = re.search(r"\b\d{6}\b", code_notice.inner_text()).group()
                page.get_by_label("Enter 6-digit code", exact=True).fill(code)
                page.get_by_role("button", name="Verify Code", exact=True).click()
                expect(page.get_by_role("heading", name="Overview and setup", exact=True)).to_be_visible(timeout=30000)
                page.get_by_role("button", name="Open offline demonstration", exact=True).click()
                expect(page.get_by_text(re.compile("Finished trials: 4 / 4"))).to_be_visible(timeout=30000)
                artifacts = ROOT / ".runtime/browser-smoke-20261102"
                artifacts.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifacts / "desktop.png"))
                page.get_by_text("Ticket workflows", exact=True).click()
                page.get_by_label("Support ticket", exact=True).fill("My fictional CarbonDesk account is locked.")
                page.get_by_role("button", name="Queue ticket", exact=True).click()
                expect(page.get_by_text(re.compile(r"Ticket queued\."))).to_be_visible()
                page.get_by_role("button", name="Cancel remaining work", exact=True).click()
                expect(page.get_by_text(re.compile(r"Cancellation saved\."))).to_be_visible()
                page.get_by_text("Comparison evidence", exact=True).click()
                page.get_by_role("button", name="Load bundled offline demonstration", exact=True).click()
                expect(page.get_by_text(re.compile("Finished trials: 4 / 4"))).to_be_visible(timeout=30000)
                page.get_by_text("Review inbox", exact=True).click()
                expect(page.get_by_text(re.compile("Pending: .*Approved:"))).to_be_visible(timeout=30000)
                page.get_by_role("button", name="Approve calibration judgment", exact=True).click()
                expect(page.get_by_text(re.compile(r"Approval was not saved\."))).to_be_visible()
                page.set_viewport_size({"width": 420, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth") <= 440
                page.screenshot(path=str(artifacts / "mobile.png"))
                page.set_viewport_size({"width": 1280, "height": 900})
                # Viewport changes can collapse the native Streamlit sidebar.
                logout = page.get_by_role("button", name="Logout", exact=True)
                collapsed = page.get_by_test_id("stSidebarCollapsedControl")
                if collapsed.is_visible():
                    collapsed.get_by_role("button").click()
                expect(logout).to_be_in_viewport(timeout=10000)
                logout.click()
                expect(page.get_by_role("heading", name=re.compile("Secure Login"))).to_be_visible()
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
