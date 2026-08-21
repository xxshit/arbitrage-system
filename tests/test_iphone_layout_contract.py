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
        toggle = function_source("toggleMobileNav", "syncMobileChatLayoutState")
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
        self.assertIn("window.visualViewport?.addEventListener('resize',()=>{syncMobileViewportHeight();syncMobileChatLayoutState()})", APP_JS)
        self.assertIn("window.visualViewport?.addEventListener('scroll',syncMobileViewportHeight)", APP_JS)
        self.assertIn("mobileViewportBaseline-height>120", APP_JS)
        self.assertIn("mobile&&chatActive&&composerFocused", APP_JS)
        self.assertIn("document.body.classList.toggle('mobile-chat-keyboard',keyboardOpen)", APP_JS)
        self.assertIn("document.addEventListener('focusin'", APP_JS)
        self.assertIn("--mobile-viewport-offset-top", STYLE_CSS)
        self.assertIn("body.mobile-chat-keyboard{position:fixed;top:var(--mobile-viewport-offset-top,0px)", STYLE_CSS)
        self.assertIn("body.mobile-chat-keyboard main{position:absolute;inset:0;height:100%", STYLE_CSS)
        self.assertIn("body.mobile-chat-keyboard #chat .chat-composer{flex:0 0 auto;padding-bottom:6px}", STYLE_CSS)
        self.assertIn("height:calc(var(--mobile-viewport-height,100dvh) - 158px", STYLE_CSS)

    def test_home_screen_chat_requires_and_recovers_audio_activation(self):
        self.assertIn('id="chatAudioGate"', INDEX_HTML)
        self.assertIn('onclick="activateChatAudio()"', INDEX_HTML)
        self.assertIn("function isStandaloneWebApp", APP_JS)
        self.assertIn("function ensureSignalAudioContext", APP_JS)
        self.assertIn("function activateChatAudio", APP_JS)
        self.assertIn("chatSoundPending=true", APP_JS)
        self.assertIn("document.addEventListener('touchend',primeSignalAudio", APP_JS)
        foreground = function_source("chatConversationIsForeground", "announceIncomingChatMessage")
        self.assertIn("mobile-conversation-open", foreground)
        announcement = function_source("announceIncomingChatMessage", "observeIncomingChatMessages")
        self.assertIn("playChatSound()", announcement)
        self.assertIn("if(!chatConversationIsForeground", announcement)
        self.assertIn("relevant=chatSoundEnabled||isStandaloneWebApp()||isAppleTouchDevice()", APP_JS)

    def test_portrait_chat_owns_the_visual_viewport(self):
        self.assertIn("@media(max-width:760px) and (orientation:portrait)", STYLE_CSS)
        self.assertIn("body.mobile-chat-view main>header", STYLE_CSS)
        self.assertIn("body.mobile-chat-view #chat.active{display:grid", STYLE_CSS)
        self.assertIn("body.mobile-chat-conversation #chat.active{grid-template-rows:auto minmax(0,1fr)}", STYLE_CSS)
        self.assertIn("body.mobile-chat-conversation #chat>.panel-title{display:none!important}", STYLE_CSS)
        self.assertIn(".chat-shell.mobile-conversation-open .chat-conversation{display:flex;flex-direction:column}", STYLE_CSS)
        self.assertIn("body.mobile-chat-view #chat .chat-messages{flex:1 1 auto}", STYLE_CSS)
        self.assertIn("body.mobile-chat-view #chat .chat-shell{height:100%;min-height:0!important", STYLE_CSS)

    def test_mobile_opportunity_boards_collapse_without_page_overflow(self):
        self.assertIn(".opportunity-signal-grid .momentum-table-head,.oi-market-table-head{display:none}", STYLE_CSS)
        self.assertIn(".opportunity-signal-grid .momentum-row{grid-template-columns:24px minmax(0,1fr) 54px", STYLE_CSS)
        self.assertIn(".oi-market-row{grid-template-columns:24px minmax(0,1fr) 70px", STYLE_CSS)
        self.assertIn('content:"三所持仓"', STYLE_CSS)
        self.assertIn('content:"参考市值"', STYLE_CSS)

    def test_accounts_use_cryptographic_desktop_and_mobile_bindings(self):
        self.assertIn("arbiscope-device-identity-v1", AUTH_HTML)
        self.assertIn("crypto.subtle.generateKey", AUTH_HTML)
        self.assertIn("namedCurve:'P-256'},false,['sign','verify']", AUTH_HTML)
        self.assertIn("/api/auth/device/challenge", AUTH_HTML)
        self.assertIn("response.status===428&&data.device_proof_required", AUTH_HTML)
        self.assertIn('data-view="security-notifications"', INDEX_HTML)
        self.assertIn('id="securityAlertBadge"', INDEX_HTML)
        self.assertIn("function accountDeviceSlot", APP_JS)
        self.assertIn("function accountLevelControl", APP_JS)
        self.assertIn("function setAccountLevel", APP_JS)
        self.assertIn("function loadSecurityAlerts", APP_JS)
        self.assertIn("account-level-badge", INDEX_HTML)
        self.assertIn("account-level-guide", INDEX_HTML)
        self.assertIn("last_login_ip", APP_JS)
        self.assertIn("source_ip", APP_JS)
        self.assertIn("20260821-account-lv123", INDEX_HTML)
        self.assertIn("可用基本功能", INDEX_HTML)
        self.assertIn("可用大部分功能", INDEX_HTML)
        self.assertIn("最高权限", INDEX_HTML)
        self.assertIn("account-device-grid", STYLE_CSS)
        self.assertIn("account-level-control", STYLE_CSS)
        self.assertIn("account-level-guide", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
