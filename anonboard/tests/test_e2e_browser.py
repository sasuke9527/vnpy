"""Drives the web client in headless Chromium against a live server to prove
that WebCrypto ECDSA signing interoperates with the Python verifier and that
the per-thread alias rules hold end to end. Skipped unless Playwright and a
Chromium build are available."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = dict(os.environ, ANONBOARD_DB=str(tmp_path / "e2e.db"), ANONBOARD_ADMIN_TOKEN="t", ANONBOARD_PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-m", "anonboard"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError(proc.stdout.read().decode())
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()


def test_browser_flow(server):
    with playwright.sync_playwright() as p:
        exe = "/opt/pw-browsers/chromium"
        try:
            browser = p.chromium.launch(executable_path=exe) if os.path.exists(exe) else p.chromium.launch()
        except Exception as exc:  # no browser in this environment
            pytest.skip(f"chromium unavailable: {exc}")
        alice, bob = browser.new_context().new_page(), browser.new_context().new_page()

        alice.goto(server + "/")
        alice.wait_for_selector("#newHint:not(:empty)")
        alice.fill("#newBody", "第一个帖子")
        alice.click("#newPost")
        alice.wait_for_selector("#threads .card")
        alice.click("#threads .card")
        alice.wait_for_selector("#threadCard .body")
        alice.fill("#commentBody", "楼主自己回复")
        alice.click("#commentPost")
        alice.wait_for_selector("#comments .comment")
        tid = alice.evaluate("location.hash.slice(1)")

        bob.goto(f"{server}/#{tid}")
        bob.wait_for_selector("#threadCard .body")
        preview = bob.text_content("#myAlias").strip()
        for i, text in enumerate(["路人第一条", "路人第二条"], start=2):
            bob.fill("#commentBody", text)
            bob.click("#commentPost")
            bob.wait_for_function(f"document.querySelectorAll('#comments .comment').length == {i}")
        names = bob.eval_on_selector_all("#comments .comment .meta > span:nth-child(2)", "els => els.map(e => e.textContent)")
        assert names[1] == names[2] != names[0], names
        assert preview.startswith(names[1]), (preview, names)
        assert bob.eval_on_selector_all("#comments .comment .badge.op", "els => els.length") == 1
        assert bob.eval_on_selector_all("#comments .comment .badge.me", "els => els.length") == 2

        bob.click("#back")
        bob.wait_for_selector("#newBody")
        bob.fill("#newBody", "第二个帖子")
        bob.click("#newPost")
        bob.wait_for_function("document.querySelectorAll('#threads .card').length == 2")
        bob.click("#threads .card")
        bob.wait_for_selector("#threadCard .body")
        op2 = bob.text_content("#threadCard .meta > span:nth-child(2)").strip()
        assert op2 != names[1], json.dumps({"t1": names, "t2": op2}, ensure_ascii=False)
        browser.close()
