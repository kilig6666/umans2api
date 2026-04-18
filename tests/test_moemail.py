import unittest

from umans2api import moemail


class MoeMailTests(unittest.TestCase):
    def test_parse_channels_and_round_robin(self):
        raw = '[{"id":"a","name":"A","enabled":true,"api_key":"k1","api_base":"https://a"},{"id":"b","name":"B","enabled":true,"api_key":"k2","api_base":"https://b"}]'
        channels, error = moemail.parse_channels(raw)
        self.assertEqual(error, "")
        self.assertEqual([item["id"] for item in channels], ["a", "b"])

        moemail._rr_cursor = 0
        cfg = {"MOEMAIL_CHANNELS_JSON": raw}
        first = moemail.get_channel_candidates(cfg)
        second = moemail.get_channel_candidates(cfg)
        self.assertEqual([item["id"] for item in first], ["a", "b"])
        self.assertEqual([item["id"] for item in second], ["b", "a"])

    def test_extract_verify_url_and_token(self):
        html = '<a href="https://app.umans.ai/verify-email?token=abc123">Verify</a>'
        self.assertEqual(
            moemail.extract_verify_url(html),
            'https://app.umans.ai/verify-email?token=abc123',
        )
        self.assertEqual(moemail.extract_verify_token(html), 'abc123')


if __name__ == '__main__':
    unittest.main()
