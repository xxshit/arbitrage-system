import io
import unittest
from unittest.mock import Mock

from scripts.preview_workspace_layout import PreviewHandler, preview_html


class LayoutPreviewSafetyTests(unittest.TestCase):
    def handler(self, path):
        handler = object.__new__(PreviewHandler)
        handler.path = path
        handler.wfile = io.BytesIO()
        for name in ("send_error", "send_response", "send_header", "end_headers"):
            setattr(handler, name, Mock())
        return handler

    def test_preview_is_explicitly_synthetic_and_mocks_network_before_business_script(self):
        html = preview_html().decode()
        self.assertIn("合成数据 · 禁止写入", html)
        self.assertLess(html.index('/layout-fixtures.js'), html.index('/static/app.js'))
        self.assertIn('/static/workspace-layout.css', html)
        self.assertIn('/static/workspace-layout.js', html)
        self.assertIn('布局预览', html)

    def test_real_api_routes_are_not_forwarded(self):
        handler = self.handler('/api/spot-futures')
        handler.do_GET()
        handler.send_error.assert_called_once_with(404)
        handler.send_response.assert_not_called()

    def test_all_write_methods_are_rejected(self):
        for method in ('do_POST', 'do_PATCH', 'do_DELETE', 'do_PUT'):
            handler = self.handler('/api/hedge-positions')
            getattr(handler, method)()
            self.assertEqual(handler.send_error.call_args.args[0], 405)
            handler.send_response.assert_not_called()

    def test_static_directory_traversal_cannot_read_project_files(self):
        for path in ('/static/../app.py', '/static/../../.env', '/.env'):
            handler = self.handler(path)
            handler.do_GET()
            handler.send_error.assert_called_once_with(404)
            self.assertEqual(handler.wfile.getvalue(), b'')

    def test_layout_assets_are_uncached_and_do_not_require_application_import(self):
        handler = self.handler('/static/workspace-layout.css?v=qa')
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call('Cache-Control', 'no-store')
        self.assertIn(b'.layout-view#onchain-opportunities', handler.wfile.getvalue())


if __name__ == '__main__':
    unittest.main()
