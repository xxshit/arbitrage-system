import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.preview_workspace_layout import TEMPLATES, preview_html


ROOT = Path(__file__).parents[1]
CSS = (ROOT / 'static/workspace-layout.css').read_text()


class HeaderAccountLayoutTests(unittest.TestCase):
    def test_account_has_one_sidebar_home_below_brand_before_navigation_search(self):
        html = preview_html().decode()
        aside = html.split('<aside id="appSidebar">', 1)[1].split('</aside>', 1)[0]
        header = html.split('<main>', 1)[1].split('</header>', 1)[0]
        self.assertLess(aside.index('class="brand"'), aside.index('class="account-chip sidebar-account"'))
        self.assertLess(aside.index('class="account-chip sidebar-account"'), aside.index('class="section-search"'))
        self.assertEqual(html.count('onclick="logoutAccount()"'), 1)
        self.assertIn('<button type="button" onclick="logoutAccount()">退出</button>', aside)
        self.assertNotIn('account-chip', header)
        self.assertNotIn('logoutAccount()', header)

    def test_refresh_precedes_sound_in_dom_and_retains_unique_live_controls(self):
        header = preview_html().decode().split('<main>', 1)[1].split('</header>', 1)[0]
        self.assertLess(header.index('class="header-refresh"'), header.index('class="sound-actions"'))
        for identity in ['updated', 'sectionRefreshButton', 'sectionRefreshFeedback', 'alarmSoundToggle', 'chatSoundToggle']:
            self.assertEqual(header.count(f'id="{identity}"'), 1, identity)
        self.assertIn('class="status"><span class="pulse">', header)
        self.assertIn('onclick="refreshCurrentSection()"', header)
        self.assertIn('onchange="toggleAlarmSound(this.checked)"', header)
        self.assertIn('onchange="toggleChatSound(this.checked)"', header)
        self.assertIn('onclick="testAlarmSound()"', header)
        self.assertIn('class="header-refresh-feedback" role="status"', header)

    def test_admin_and_viewer_labels_and_permissions_keep_existing_template_rules(self):
        template = TEMPLATES.get_template('index.html')
        for role, level, label in [('admin', 'lv3', '管理员'), ('viewer', 'lv1', '查看账号'), ('viewer', 'lv2', '查看账号')]:
            html = template.render(
                current_user=SimpleNamespace(username='长账号' * 40 + '<script>unsafe</script>', role=role, chat_nav_hidden=False),
                account_level=level,
                url_for=lambda endpoint, filename=None: f'/static/{filename}' if filename else '/manifest',
            )
            aside = html.split('<aside id="appSidebar">', 1)[1].split('</aside>', 1)[0]
            self.assertIn(f'<b>{label}</b>', aside)
            self.assertIn(f'>{level.upper()}</em>', aside)
            self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', aside)
            self.assertNotIn('<script>unsafe</script>', aside)
            self.assertEqual('data-view="account-management"' in aside, role == 'admin')
            self.assertEqual('data-view="security-notifications"' in aside, role == 'admin')

    def test_narrow_sidebar_names_wrap_but_logout_remains_reachable(self):
        self.assertIn('aside#appSidebar .sidebar-account-info {display:grid;gap:6px;min-width:0}', CSS)
        self.assertRegex(CSS, r'\.sidebar-account-info > span \{[^}]*overflow-wrap:anywhere')
        self.assertRegex(CSS, r'\.sidebar-account button \{flex:0 0 auto;[^}]*white-space:nowrap')
        self.assertIn('aside#appSidebar .sidebar-account button {min-height:40px}', CSS)
        self.assertIn('.sidebar-account button:focus-visible', CSS)
        self.assertIn('aside#appSidebar .sidebar-account-meta > b {display:inline-block}', CSS)

    def test_header_can_wrap_without_clipping_status_or_reordering_with_css(self):
        self.assertIn('.workspace-layout .header-actions {display:flex;flex-direction:row;flex-wrap:wrap;', CSS)
        self.assertIn('.workspace-layout .header-actions .header-refresh {width:100%}', CSS)
        self.assertIn('.workspace-layout .header-refresh #updated {min-width:0;overflow-wrap:anywhere}', CSS)
        self.assertNotIn('.header-actions .account-chip', CSS)
        refresh_rules = re.findall(r'[^{}]*\.header-refresh[^{}]*\{([^{}]*)\}', CSS)
        self.assertTrue(refresh_rules)
        self.assertTrue(all('order:' not in rule and 'overflow:hidden' not in rule for rule in refresh_rules))


if __name__ == '__main__':
    unittest.main()
