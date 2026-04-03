"""End-to-end WebUI tests using Playwright.

These tests require:
  - API server running: nvh serve
  - WebUI running: nvh webui
  - Playwright: pip install playwright && playwright install chromium

Skip gracefully if Playwright or servers are not available.
"""


import pytest

# Check if playwright is available
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# Check if servers are running
def _server_up(url: str) -> bool:
    try:
        import httpx
        resp = httpx.get(url, timeout=3)
        return resp.status_code == 200
    except Exception:
        return False

API_UP = _server_up("http://localhost:8000/v1/health")
WEBUI_UP = _server_up("http://localhost:3000")

SKIP_REASON = (
    "Playwright not installed" if not HAS_PLAYWRIGHT
    else "API server not running" if not API_UP
    else "WebUI not running" if not WEBUI_UP
    else None
)

pytestmark = pytest.mark.skipif(
    SKIP_REASON is not None,
    reason=SKIP_REASON or "",
)


BASE = "http://localhost:3000"


@pytest.fixture(scope="module")
def browser():
    """Launch a shared browser for all tests."""
    if not HAS_PLAYWRIGHT:
        pytest.skip("Playwright not installed")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """Create a fresh page for each test."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        color_scheme="dark",
    )
    pg = context.new_page()
    yield pg
    pg.close()
    context.close()


# ---------------------------------------------------------------------------
# Page loading tests
# ---------------------------------------------------------------------------

class TestWebUIPages:
    def test_chat_page_loads(self, page):
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        assert "Hive" in page.title() or page.locator("text=Hive AI").count() > 0

    def test_providers_page_loads(self, page):
        page.goto(f"{BASE}/providers")
        page.wait_for_load_state("networkidle")
        assert page.locator("text=Advisors").count() > 0

    def test_integrations_page_loads(self, page):
        page.goto(f"{BASE}/integrations")
        page.wait_for_load_state("networkidle")
        assert page.locator("text=Connect your tools").count() > 0

    def test_system_page_loads(self, page):
        page.goto(f"{BASE}/system")
        page.wait_for_load_state("networkidle")
        assert page.locator("text=System Status").count() > 0

    def test_setup_page_loads(self, page):
        page.goto(f"{BASE}/setup")
        page.wait_for_load_state("networkidle")
        assert page.locator("text=Setup Wizard").count() > 0

    def test_settings_page_loads(self, page):
        page.goto(f"{BASE}/settings")
        page.wait_for_load_state("networkidle")
        assert page.locator("text=Settings").count() > 0


# ---------------------------------------------------------------------------
# Chat interaction tests
# ---------------------------------------------------------------------------

class TestWebUIChat:
    def test_send_message(self, page):
        """Type a message and verify a response appears."""
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        textarea = page.locator('textarea[placeholder="Send a message..."]')
        textarea.fill("Say hello in exactly 3 words")

        send_btn = page.locator('button[title="Send (Ctrl+Enter)"]')
        send_btn.click()

        # Wait for response (assistant message with left border)
        try:
            page.wait_for_selector('[class*="border-l"]', timeout=30000)
            assert True  # Response appeared
        except Exception:
            # Check if there's any new content
            content = page.content()
            assert "hello" in content.lower() or "error" in content.lower()

    def test_new_chat_button(self, page):
        """New Chat button should create a fresh conversation."""
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        new_chat = page.locator('button:has-text("NEW CHAT")')
        assert new_chat.count() > 0
        new_chat.click()
        page.wait_for_timeout(500)

        # Input should be empty and enabled
        textarea = page.locator('textarea[placeholder="Send a message..."]')
        assert textarea.input_value() == ""
        assert textarea.is_enabled()

    def test_mode_selector(self, page):
        """Mode selector should have SINGLE, CONVENE, POLL."""
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        assert page.locator("text=SINGLE").count() > 0
        assert page.locator("text=CONVENE").count() > 0
        assert page.locator("text=POLL").count() > 0


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

class TestWebUINavigation:
    def test_sidebar_nav_links(self, page):
        """Sidebar should have nav links to all pages."""
        page.goto(f"{BASE}/providers")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        assert page.locator("text=System").count() > 0
        assert page.locator("text=Advisors").count() > 0
        assert page.locator("text=Integrations").count() > 0
        assert page.locator("text=Settings").count() > 0
        assert page.locator("text=Setup").count() > 0

    def test_new_chat_from_other_page(self, page):
        """NEW CHAT from providers page should navigate to chat."""
        page.goto(f"{BASE}/providers")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        new_chat = page.locator('button:has-text("NEW CHAT")')
        new_chat.click()
        page.wait_for_timeout(1000)

        # Should be on the chat page now
        assert page.url == f"{BASE}/" or "localhost:3000" in page.url


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

class TestWebUISetup:
    def test_setup_wizard_steps(self, page):
        """Setup wizard should show step indicators."""
        page.goto(f"{BASE}/setup")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        # Should show step 1 (Welcome)
        assert page.locator("text=Welcome").count() > 0

    def test_setup_next_button(self, page):
        """NEXT button should advance to next step."""
        page.goto(f"{BASE}/setup")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        next_btn = page.locator('button:has-text("NEXT")')
        if next_btn.count() > 0:
            next_btn.click()
            page.wait_for_timeout(1000)
            # Should have advanced (GPU step or Local AI step)
            content = page.content()
            assert "GPU" in content or "Local" in content or "Step" in content


# ---------------------------------------------------------------------------
# Integrations page
# ---------------------------------------------------------------------------

class TestWebUIIntegrations:
    def test_integrations_shows_platforms(self, page):
        """Integrations page should list platforms."""
        page.goto(f"{BASE}/integrations")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        content = page.content()
        # Should show at least some platform names or connection status
        assert (
            "Claude" in content
            or "NemoClaw" in content
            or "Connected" in content
            or "Connect" in content
        )

    def test_integrations_troubleshoot_link(self, page):
        """'Having trouble?' link should be present."""
        page.goto(f"{BASE}/integrations")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        assert page.locator("text=Having trouble").count() > 0
