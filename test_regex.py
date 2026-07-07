import unittest

from backend.tasks import clean_hallucinated_loops


class HallucinationCleanupTests(unittest.TestCase):
    def test_repeated_phrase_tail_is_replaced_with_system_note(self):
        text = "這是一段正常的會議記錄。" + "那個，" * 12

        cleaned = clean_hallucinated_loops(text)

        self.assertTrue(cleaned.startswith("這是一段正常的會議記錄。"))
        self.assertIn("[系統提示：此處音檔包含無意義雜訊", cleaned)

    def test_normal_text_is_unchanged(self):
        text = "今天討論預算、時程與下次會議安排。"

        self.assertEqual(clean_hallucinated_loops(text), text)


if __name__ == "__main__":
    unittest.main()
