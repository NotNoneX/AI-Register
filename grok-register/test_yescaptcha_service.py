import os
import unittest
from unittest.mock import patch

from YesCaptcha_service import TurnstileService


class YesCaptchaDeveloperParameterTests(unittest.TestCase):
    @patch("YesCaptcha_service.requests.post")
    def test_create_task_includes_developer_soft_id(self, post):
        post.return_value.json.return_value = {
            "errorId": 0,
            "taskId": "turnstile-task",
        }

        with patch.dict(os.environ, {"YESCAPTCHA_KEY": "test-client-key"}):
            task_id = TurnstileService().create_task(
                "https://example.test/",
                "site-key",
            )

        self.assertEqual(task_id, "turnstile-task")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["softID"], "102154")
        self.assertNotIn("softID", payload["task"])


if __name__ == "__main__":
    unittest.main()
