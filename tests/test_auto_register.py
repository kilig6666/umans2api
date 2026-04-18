import unittest
from unittest import mock

from umans2api import auto_register


class DummyAccounts:
    def count_active_accounts(self):
        return 1


class AutoRegisterTests(unittest.TestCase):
    def setUp(self):
        auto_register.configure(
            get_config=lambda: {
                'MAIL_PROVIDER_DEFAULT': 'moemail',
                'MOEMAIL_API_KEY': 'key',
                'MOEMAIL_API_BASE': 'https://mail.example.com',
                'AUTO_REGISTER_BROWSER_MODE_MANUAL': 'visible',
                'AUTO_REGISTER_BROWSER_MODE_BACKGROUND': 'headless',
                'AUTO_REGISTER_ENABLED': True,
                'AUTO_RELOGIN_ENABLED': True,
                'AUTO_REGISTER_MIN_ACTIVE': 1,
            },
            account_manager=DummyAccounts(),
            keepalive=object(),
            logger=None,
        )

    @mock.patch('umans2api.auto_register.browser_auth.is_available', return_value=(True, ''))
    def test_check_config_ready(self, _mock_available):
        result = auto_register.check_config()
        self.assertTrue(result['ready'])
        self.assertEqual(result['mail_provider_default'], 'moemail')
        self.assertEqual(result['manual_browser_modes'], ['visible', 'headless'])

    @mock.patch('umans2api.auto_register._run_task')
    @mock.patch('umans2api.auto_register.browser_auth.is_available', return_value=(True, ''))
    def test_start_visible_forces_single_worker(self, _mock_available, _mock_run_task):
        task, err = auto_register.start(count=2, workers=2, browser_mode='visible')
        self.assertIsNone(err)
        self.assertEqual(task['workers'], 1)
        auto_register.stop(task['id'])


if __name__ == '__main__':
    unittest.main()
