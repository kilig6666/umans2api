import importlib
import json
import os
import unittest
from unittest import mock

os.environ['UMANS2API_DISABLE_BACKGROUND_THREADS'] = '1'
appmod = importlib.import_module('umasn2api')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_authed'] = True

    def test_auto_register_config_route(self):
        with mock.patch.object(appmod.auto_register, 'check_config', return_value={'ready': True, 'current_task': None}):
            resp = self.client.get('/api/auto-register/config')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ready'])

    def test_auto_register_start_route(self):
        with mock.patch.object(appmod.auto_register, 'start', return_value=({'id': 'task-1', 'status': 'running', 'workers': 1}, None)), \
             mock.patch.object(appmod.auto_register, 'check_config', return_value={'ready': True, 'current_task': {'id': 'task-1'}}):
            resp = self.client.post('/api/auto-register/start', json={'count': 1, 'workers': 2, 'browser_mode': 'visible'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['task']['id'], 'task-1')

    def test_auto_register_stream_route(self):
        running = {
            'id': 'task-1', 'status': 'running', 'requested': 1, 'success_count': 0, 'failed_count': 0,
            'workers': 1, 'logs': [{'seq': 1, 'ts': 1, 'level': 'INFO', 'message': 'start'}], 'results': []
        }
        done = {
            'id': 'task-1', 'status': 'completed', 'requested': 1, 'success_count': 1, 'failed_count': 0,
            'workers': 1, 'logs': [{'seq': 1, 'ts': 1, 'level': 'INFO', 'message': 'start'}],
            'results': [{'seq': 1, 'email': 'user@example.com'}]
        }
        with mock.patch.object(appmod.auto_register, 'get_current_task', side_effect=[running, done]):
            resp = self.client.get('/api/auto-register/stream?task_id=task-1')
            payload = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('event: state', payload)
        self.assertIn('event: log', payload)
        self.assertIn('event: result', payload)
        self.assertIn('event: done', payload)

    def test_relogin_route(self):
        with mock.patch.object(appmod.KEEPALIVE, 'relogin_account', return_value={'ok': True, 'data': {'user': {'email': 'a@example.com'}}}):
            resp = self.client.post('/api/accounts/acc1/relogin', json={'browser_mode': 'headless'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ok'])


if __name__ == '__main__':
    unittest.main()
