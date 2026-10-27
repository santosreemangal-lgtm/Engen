import unittest

from engine.logger import redact


class ExtendedRedactionTests(unittest.TestCase):
    def test_bearer_credentials_are_redacted(self):
        message = "request failed Authorization: Bearer abc.DEF-123"
        result = redact(message)
        self.assertNotIn("abc.DEF-123", result)
        self.assertIn("Bearer [REDACTED]", result)

    def test_url_query_credentials_are_redacted(self):
        message = "request failed at https://rpc.example/?api-key=supersecret&token=abc123"
        result = redact(message)
        self.assertNotIn("supersecret", result)
        self.assertNotIn("abc123", result)
        self.assertIn("api-key=[REDACTED]", result)
        self.assertIn("token=[REDACTED]", result)

    def test_secret_assignment_does_not_consume_punctuation(self):
        result = redact("token=abc123, retrying")
        self.assertEqual(result, "token=[REDACTED], retrying")


if __name__ == "__main__":
    unittest.main()
