"""Optional mocked Chromium preview; needs Playwright and an installed Chromium."""

import json
import logging
import sys
import threading
from pathlib import Path

from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]


def snapshot(kind: str) -> dict:
    common = {"state": "responding", "user_final": True, "assistant_final": True,
              "playback": True, "error": ""}
    if kind == "weather":
        return {**common, "user_text": "What is the weather in Auckland?",
                "assistant_text": "Auckland is rainy and 11 degrees Celsius, according to Open-Meteo.",
                "tool": {"version": 1, "turn_id": "u1", "call_id": "c1", "response_id": "r1",
                         "kind": "weather_current", "status": "completed",
                         "title": "Weather · Auckland, New Zealand",
                         "summary": "Rain · 11°C · Feels 8°C",
                         "detail": "Current model estimate · 70% rain chance",
                         "freshness": "fresh", "sources": []}}
    return {**common, "user_text": "What is the latest Raspberry Pi OS release?",
            "assistant_text": "Raspberry Pi reports a new release; see the cited sources.",
            "tool": {"version": 1, "turn_id": "u2", "call_id": "c2", "response_id": "r2",
                     "kind": "web_search", "status": "completed",
                     "title": "Latest Raspberry Pi OS release",
                     "summary": "Raspberry Pi published a release note [1]. Another source confirms it [2].",
                     "detail": "Cited web result", "freshness": "",
                     "sources": [
                         {"title": "Raspberry Pi release note", "url": "https://www.raspberrypi.com/news/release/",
                          "source_name": "www.raspberrypi.com", "publication_date": "2026-11-02"},
                         {"title": "Technology report", "url": "https://example.org/report",
                          "source_name": "example.org", "publication_date": "2026-11-02"}]}}


def check(page, expected_kind: str) -> None:
    panel = page.locator('[data-role="assistant-tool"]')
    panel.wait_for(state="visible")
    assert panel.get_attribute("data-kind") == expected_kind
    for name in ("weather", "news", "pihole", "system"):
        assert page.locator(f"#module-{name}").count() == 1
    dimensions = page.evaluate("""({width: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        height: document.documentElement.scrollHeight,
        clientHeight: document.documentElement.clientHeight})""")
    assert dimensions["width"] <= dimensions["clientWidth"], dimensions
    assert dimensions["height"] <= dimensions["clientHeight"], dimensions


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from magic_mirror import create_app
    from magic_mirror.config import Settings
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install optional playwright==1.62.0 in a development virtual environment") from exc
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, create_app(Settings(wake_enabled=True)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                                        args=["--no-sandbox"])
            try:
                url = f"http://127.0.0.1:{server.server_port}/"
                for name, kind in (("weather", "weather_current"), ("search", "web_search")):
                    page = browser.new_page(viewport={"width": 600, "height": 1024})
                    value = snapshot(name)

                    def route_assistant(route, _request=None, data=value):
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

                    page.route("**/api/assistant", route_assistant)
                    page.goto(url, wait_until="networkidle")
                    check(page, kind)
                    page.reload(wait_until="networkidle")
                    check(page, kind)
                    if name == "search":
                        assert page.locator('[data-role="tool-sources"] a').count() == 2
                    path = ROOT / "docs" / f"pass7d-{name}-portrait.png"
                    page.screenshot(path=str(path))
                    path.chmod(0o644)
                    page.close()
                page = browser.new_page(viewport={"width": 1024, "height": 600})
                data = snapshot("search")
                data["tool"]["summary"] = "<script>window.bad=true</script>"
                data["tool"]["sources"][0]["title"] = "<img src=x onerror=alert(1)>"
                data["tool"]["sources"].append({"title": "Private", "url": "javascript:alert(1)"})

                def route_malicious(route, _request=None):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

                page.route("**/api/assistant", route_malicious)
                page.goto(url, wait_until="networkidle")
                check(page, "web_search")
                assert page.locator("script:has-text('window.bad=true')").count() == 0
                assert page.locator('[data-role="tool-sources"] img').count() == 0
                assert page.locator('[data-role="tool-sources"] a').count() == 2
                assert page.locator('[data-role="tool-summary"]').inner_text() == data["tool"]["summary"]
                page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    print("Pass 7D mocked Chromium checks passed at 600×1024 and 1024×600.")


if __name__ == "__main__":
    main()
