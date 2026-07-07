import unittest

from google.genai import types


class GeminiConfigTests(unittest.TestCase):
    def test_generate_content_config_accepts_penalty_settings(self):
        config = types.GenerateContentConfig(
            temperature=0.1,
            frequency_penalty=2.0,
            presence_penalty=2.0,
        )

        self.assertEqual(config.temperature, 0.1)
        self.assertEqual(config.frequency_penalty, 2.0)
        self.assertEqual(config.presence_penalty, 2.0)


if __name__ == "__main__":
    unittest.main()
