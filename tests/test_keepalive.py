import unittest
from unittest import mock

from umans2api.keepalive import KeepAliveService


class FakeAccountManager:
    def __init__(self):
        self.mark_fail_calls = []
        self.update_session_calls = []

    def get_account(self, *args, **kwargs):
        return {'id': 'acc1', 'email': 'a@example.com', 'password': 'secret', 'cookies': {}}

    def mark_fail(self, account_id, message, auth_invalid=False):
        self.mark_fail_calls.append((account_id, message, auth_invalid))

    def update_session_info(self, account_id, **kwargs):
        self.update_session_calls.append((account_id, kwargs))

    def update_account(self, *args, **kwargs):
        return None

    def update_relogin_result(self, *args, **kwargs):
        return None

    def merge_response_cookies(self, *args, **kwargs):
        return None

    def touch_keepalive(self, *args, **kwargs):
        return None


class KeepAliveReloginTests(unittest.TestCase):
    def setUp(self):
        self.accounts = FakeAccountManager()
        self.service = KeepAliveService(self.accounts, lambda: {'AUTO_RELOGIN_ENABLED': True, 'upstream_url': 'https://app.umans.ai/api/chat'}, mock.Mock(), 'ua')

    def test_auth_invalid_relogin_success(self):
        with mock.patch.object(self.service, '_fetch_session', side_effect=[RuntimeError('session missing user'), {'user': {'email': 'a@example.com'}, 'expires': '2099-01-01T00:00:00Z'}]), \
             mock.patch.object(self.service, 'relogin_account', return_value={'ok': True, 'data': {'user': {'email': 'a@example.com'}}}):
            result = self.service.check_account('acc1')
        self.assertTrue(result['ok'])
        self.assertEqual(self.accounts.mark_fail_calls, [])

    def test_auth_invalid_relogin_fail_marks_account(self):
        with mock.patch.object(self.service, '_fetch_session', side_effect=RuntimeError('session missing user')), \
             mock.patch.object(self.service, 'relogin_account', side_effect=RuntimeError('bad login')):
            result = self.service.check_account('acc1')
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.accounts.mark_fail_calls), 1)
        self.assertIn('relogin failed', self.accounts.mark_fail_calls[0][1])


if __name__ == '__main__':
    unittest.main()
