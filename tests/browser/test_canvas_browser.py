"""Browser checks against a disposable running Compose stack."""

import subprocess
import os

import pytest
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


pytestmark = [pytest.mark.browser, pytest.mark.skipif(
    os.getenv("RUN_BROWSER_INTEGRATION") != "1", reason="browser tests are opt-in"
)]


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto("http://localhost:8000/")
        page.get_by_text("Connected — live collaboration enabled").wait_for(timeout=20000)
        yield page
        browser.close()


def draw(page, x, y, color):
    result = page.evaluate("""async ({x, y, color}) => {
        const response = await fetch('/draw', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({x, y, color})
        });
        return {status: response.status, body: await response.json()};
    }""", {"x": x, "y": y, "color": color})
    assert result["status"] == 200, result


def color_at(page, x, y):
    return page.locator("canvas").evaluate(
        "(canvas, point) => Array.from(canvas.getContext('2d').getImageData(point.x, point.y, 1, 1).data)",
        {"x": x, "y": y},
    )[:3]


def test_adjacent_pixels_and_clear_at_zoom(page):
    draw(page, 25, 0, "#ff0000")
    draw(page, 26, 0, "#0000ff")
    page.wait_for_function("""() => {
        const c = document.querySelector('canvas').getContext('2d');
        return c.getImageData(25, 0, 1, 1).data[0] === 255 &&
               c.getImageData(26, 0, 1, 1).data[2] === 255;
    }""")
    page.locator("#zoom").fill("4")
    assert color_at(page, 25, 0) == [255, 0, 0]
    assert color_at(page, 26, 0) == [0, 0, 255]
    page.reload()
    page.get_by_text("Connected — live collaboration enabled").wait_for(timeout=20000)
    assert color_at(page, 25, 0) == [255, 0, 0]
    assert color_at(page, 26, 0) == [0, 0, 255]
    page.locator("#clear").click()
    page.wait_for_function("""() => {
        const c = document.querySelector('canvas').getContext('2d');
        return c.getImageData(25, 0, 1, 1).data[0] === 255 &&
               c.getImageData(26, 0, 1, 1).data[2] === 255 &&
               c.getImageData(26, 0, 1, 1).data[0] === 255;
    }""")
    assert color_at(page, 25, 0) == [255, 255, 255]
    assert color_at(page, 26, 0) == [255, 255, 255]


def test_reconnect_restores_authoritative_canvas(page):
    draw(page, 25, 1, "#00ff00")
    try:
        compose("restart", "coordinator")
        page.locator("#clear:disabled").wait_for(timeout=20000)
        page.locator("#clear:enabled").wait_for(timeout=30000)
        assert color_at(page, 25, 1) == [0, 255, 0]
    finally:
        page.locator("#clear:enabled").click(timeout=30000)


def test_partial_clear_reports_error_and_recovers(page):
    draw(page, 0, 0, "#ff0000")
    try:
        compose("stop", "shard-2")
        with page.expect_response(lambda response: response.url.endswith("/clear")) as clear_response:
            page.locator("#clear").click()
        assert clear_response.value.status == 503
        page.locator("#clear:disabled").wait_for()
    finally:
        compose("start", "shard-2")
    page.locator("#clear:enabled").wait_for(timeout=30000)
    assert color_at(page, 0, 0) == [255, 255, 255]
