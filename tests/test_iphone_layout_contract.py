import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
AUTH_HTML = (ROOT / "templates" / "auth.html").read_text(encoding="utf-8")


def function_source(name, next_name):
    start = APP_JS.index(f"function {name}")
    end = APP_JS.index(f"function {next_name}", start)
    return APP_JS[start:end]


class IPhoneLayoutContractTests(unittest.TestCase):
    def test_pages_opt_into_iphone_safe_area(self):
        self.assertIn("viewport-fit=cover", INDEX_HTML)
        self.assertIn("viewport-fit=cover", AUTH_HTML)
        self.assertIn('name="theme-color"', INDEX_HTML)
        self.assertIn('name="apple-mobile-web-app-capable"', INDEX_HTML)
        self.assertIn("env(safe-area-inset-top)", STYLE_CSS)
        self.assertIn("env(safe-area-inset-bottom)", STYLE_CSS)
        self.assertIn("--mobile-viewport-height", STYLE_CSS)

    def test_mobile_drawer_keeps_full_navigation_available(self):
        self.assertIn('id="mobileNavToggle"', INDEX_HTML)
        self.assertIn('id="mobileNavBackdrop"', INDEX_HTML)
        self.assertIn('id="appSidebar"', INDEX_HTML)
        toggle = function_source("toggleMobileNav", "showMobileChatPeers")
        self.assertIn("mobile-nav-open", toggle)
        self.assertIn("aria-expanded", toggle)
        self.assertIn("aria-hidden", toggle)
        self.assertIn("aside#appSidebar", STYLE_CSS)
        self.assertIn(".mobile-nav-open aside#appSidebar", STYLE_CSS)

    def test_mobile_chat_uses_contact_then_conversation_flow(self):
        header = function_source("renderChatConversationHeader", "openChatRemarkEditor")
        select_peer = function_source("selectChatPeer", "loadChatMessages")
        load_chat = function_source("loadChat", "startChatPolling")
        self.assertIn("chat-mobile-back", header)
        self.assertIn("showMobileChatPeers()", header)
        self.assertIn("mobile-conversation-open", select_peer)
        self.assertIn("window.innerWidth>760", select_peer)
        self.assertIn("chatUsers.length&&window.innerWidth>760", load_chat)
        self.assertIn("#chat .chat-shell.mobile-conversation-open .chat-conversation", STYLE_CSS)
        self.assertIn("#chat .chat-shell.mobile-conversation-open .chat-peers", STYLE_CSS)

    def test_iphone_inputs_avoid_focus_zoom_and_chat_tracks_keyboard(self):
        self.assertIn('select,textarea{font-size:16px!important}', STYLE_CSS)
        self.assertIn("window.visualViewport?.addEventListener('resize',syncMobileViewportHeight)", APP_JS)
        self.assertIn("height:calc(var(--mobile-viewport-height,100dvh) - 158px", STYLE_CSS)

    def test_mobile_opportunity_boards_collapse_without_page_overflow(self):
        self.assertIn(".opportunity-signal-grid .momentum-table-head,.oi-market-table-head{display:none}", STYLE_CSS)
        self.assertIn(".opportunity-signal-grid .momentum-row{grid-template-columns:24px minmax(0,1fr) 54px", STYLE_CSS)
        self.assertIn(".oi-market-row{grid-template-columns:24px minmax(0,1fr) 70px", STYLE_CSS)
        self.assertIn('content:"三所持仓"', STYLE_CSS)
        self.assertIn('content:"参考市值"', STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
