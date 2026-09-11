import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from utils import extract_verification_link, generate_password, save_api_key


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeOutlookTwSession:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if url.endswith("/api/generate"):
            return FakeResponse(
                {
                    "email": "testbox@outlook.tw",
                    "expires": 123456789,
                    "anonymous": True,
                }
            )
        if url.endswith("/api/emails"):
            return FakeResponse([{"id": 42, "subject": "Verify your email"}])
        if url.endswith("/api/email/42"):
            return FakeResponse(
                {
                    "id": 42,
                    "html_content": (
                        '<a href="https://auth.tavily.com/u/email-verification?'
                        'ticket=outlook-tw-test">Verify</a>'
                    ),
                }
            )
        raise AssertionError(f"unexpected URL: {url}")

    def close(self):
        self.closed = True


class FakeCaptchaSession(FakeOutlookTwSession):
    def __init__(self):
        super().__init__()
        self.captcha_passed = False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if url.endswith("/api/generate"):
            return FakeResponse({"email": "captcha@outlook.tw", "expires": 123})
        if url.endswith("/api/emails") and not self.captcha_passed:
            return FakeResponse(
                {"error": "captcha-required", "sitekey": "test-site-key"},
                status_code=403,
            )
        if url.endswith("/api/emails"):
            return FakeResponse(
                [
                    {
                        "html_content": (
                            "https://auth.tavily.com/u/email-verification?"
                            "ticket=after-captcha"
                        )
                    }
                ]
            )
        raise AssertionError(f"unexpected URL: {url}")


class AlwaysCaptchaSession(FakeOutlookTwSession):
    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if url.endswith("/api/emails"):
            return FakeResponse(
                {"error": "captcha-required", "sitekey": "test-site-key"},
                status_code=403,
            )
        raise AssertionError(f"unexpected URL: {url}")


class FakeCaptchaSubmitSession:
    def __init__(self):
        self.post_calls = []

    def post(self, url, headers=None, timeout=None):
        self.post_calls.append((url, headers, timeout))
        return FakeResponse({"success": True})


class VerificationLinkTests(unittest.TestCase):
    def test_extract_verification_link_from_html(self):
        content = (
            '<div>Click <a href="https://auth.tavily.com/u/email-verification?'
            'ticket=abc123xyz">here</a> to verify</div>'
        )
        link = extract_verification_link(content)
        self.assertEqual(
            link,
            "https://auth.tavily.com/u/email-verification?ticket=abc123xyz",
        )

    def test_extract_verification_link_returns_none_when_missing(self):
        self.assertIsNone(extract_verification_link("No link here"))


class OutlookTwProviderTests(unittest.TestCase):
    def test_acquire_email_and_wait_for_link(self):
        from outlook_tw_provider import OutlookTwProvider

        session = FakeOutlookTwSession()
        provider = OutlookTwProvider(session=session)

        email = provider.acquire_email()
        link = provider.wait_for_verification_link()
        provider.close()

        self.assertEqual(email, "testbox@outlook.tw")
        self.assertEqual(
            link,
            "https://auth.tavily.com/u/email-verification?ticket=outlook-tw-test",
        )
        self.assertTrue(provider.completed)
        self.assertTrue(session.closed)

    def test_default_providers_reuse_one_captcha_bearing_session(self):
        import outlook_tw_provider

        outlook_tw_provider.close_shared_outlook_tw_session()
        session = FakeOutlookTwSession()
        try:
            with patch(
                "outlook_tw_provider._create_http_session",
                return_value=session,
            ):
                first = outlook_tw_provider.OutlookTwProvider()
                second = outlook_tw_provider.OutlookTwProvider()

            self.assertIs(first.session, session)
            self.assertIs(second.session, session)
            first.close()
            second.close()
            self.assertFalse(session.closed)
        finally:
            outlook_tw_provider.close_shared_outlook_tw_session()

        self.assertTrue(session.closed)

    def test_captcha_required_runs_yescaptcha_solver_once_then_retries(self):
        from outlook_tw_provider import OutlookTwProvider

        session = FakeCaptchaSession()
        solver_calls = []

        def solve(fake_session, sitekey):
            solver_calls.append(sitekey)
            fake_session.captcha_passed = True

        provider = OutlookTwProvider(session=session, captcha_solver=solve)
        self.assertEqual(provider.acquire_email(), "captcha@outlook.tw")
        self.assertEqual(
            provider.wait_for_verification_link(),
            "https://auth.tavily.com/u/email-verification?ticket=after-captcha",
        )
        self.assertEqual(
            solver_calls,
            ["test-site-key"],
        )

    def test_yescaptcha_token_is_submitted_like_outlook_frontend(self):
        from outlook_tw_captcha import complete_outlook_tw_captcha

        session = FakeCaptchaSubmitSession()
        fake_config = {"YESCAPTCHA_CLIENT_KEY": "test-client-key"}
        with (
            patch("signup.load_config", return_value=fake_config),
            patch(
                "signup.solve_turnstile_with_yescaptcha",
                return_value="solved-turnstile-token",
            ) as solve,
        ):
            complete_outlook_tw_captcha(session, "test-site-key")

        solve.assert_called_once_with(
            "test-site-key",
            "https://outlook.tw/",
            fake_config,
        )
        self.assertEqual(len(session.post_calls), 1)
        url, headers, timeout = session.post_calls[0]
        self.assertEqual(url, "https://outlook.tw/api/captcha")
        self.assertEqual(
            headers["cf-turnstile-response"],
            "solved-turnstile-token",
        )
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertGreater(timeout, 0)

    def test_rejected_yescaptcha_token_does_not_resolve_forever(self):
        from outlook_tw_captcha import OutlookTwCaptchaError
        from outlook_tw_provider import OutlookTwProvider

        session = AlwaysCaptchaSession()
        solver_calls = []
        provider = OutlookTwProvider(
            session=session,
            captcha_solver=lambda *_args: solver_calls.append(True),
        )
        provider.email = "captcha@outlook.tw"

        with self.assertRaisesRegex(
            OutlookTwCaptchaError,
            "拒绝了 YesCaptcha token",
        ):
            provider.wait_for_verification_link()

        self.assertEqual(solver_calls, [True])
        self.assertEqual(len(session.calls), 2)


class ApiKeyOutputTests(unittest.TestCase):
    def test_save_api_key_appends_clean_token(self):
        with TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "api_keys.txt"
            with patch("utils.API_KEYS_FILE", str(output_file)):
                key1 = save_api_key("tvly-dev-testtoken123")
                key2 = save_api_key("tvly-dev-testtoken123")
                key3 = save_api_key("tvly-dev-testtoken456")

            self.assertEqual(key1, "tvly-dev-testtoken123")
            self.assertEqual(key2, "tvly-dev-testtoken123")

            lines = output_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines,
                [
                    "tvly-dev-testtoken123",
                    "tvly-dev-testtoken456",
                ],
            )


class PasswordGenerationTests(unittest.TestCase):
    def test_generate_password_structure(self):
        pwd = generate_password(16)
        self.assertEqual(len(pwd), 16)
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))


class ProxyManagerTests(unittest.TestCase):
    @patch("proxy_manager.requests.get")
    def test_proxy_manager_rotation(self, mock_get):
        from proxy_manager import ProxyManager

        mock_get.return_value.status_code = 200
        mock_get.return_value.text = "192.168.1.100:8080"

        pm = ProxyManager(proxy_api_url="http://fake-api", max_attempts_per_ip=2, poll_interval=0)
        
        # 提取第一个 IP
        p1 = pm.get_proxy()
        self.assertEqual(p1, "http://192.168.1.100:8080")
        
        # 使用 1 次
        pm.record_attempt()
        self.assertEqual(pm.get_proxy(), "http://192.168.1.100:8080")
        
        # 使用第 2 次，达到最大限制
        pm.record_attempt()

        # 模拟下一个提取到的 IP 变化
        mock_get.return_value.text = "192.168.1.101:8080"
        p2 = pm.get_proxy()
        self.assertEqual(p2, "http://192.168.1.101:8080")
        self.assertEqual(pm.attempts_on_current_ip, 0)


if __name__ == "__main__":
    unittest.main()
