import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
import signup
from proxy_manager import ProxyManager


class FakeResponse:
    def __init__(self, status_code, url, *, text="", location=None):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = {}
        if location is not None:
            self.headers["Location"] = location


class FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class YesCaptchaTests(unittest.TestCase):
    def test_direct_captcha_session_ignores_environment_proxies(self):
        with patch.dict(
            "signup.os.environ",
            {
                "HTTP_PROXY": "http://unstable-proxy.test:8080",
                "HTTPS_PROXY": "http://unstable-proxy.test:8080",
                "ALL_PROXY": "socks5://unstable-proxy.test:1080",
            },
        ):
            session = signup._create_direct_captcha_session()
            settings = session.merge_environment_settings(
                "https://api.yescaptcha.com/createTask",
                {},
                None,
                None,
                None,
            )
            session.close()

        self.assertFalse(session.trust_env)
        self.assertEqual(settings["proxies"], {})

    @patch("signup.time.sleep", return_value=None)
    @patch("signup.external_request_with_retry")
    def test_turnstile_create_task_includes_developer_soft_id(
        self,
        request_with_retry,
        _sleep,
    ):
        request_with_retry.side_effect = [
            FakeJsonResponse({"errorId": 0, "taskId": "turnstile-task"}),
            FakeJsonResponse(
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"token": "turnstile-token"},
                }
            ),
        ]

        token = signup.solve_turnstile_with_yescaptcha(
            "site-key",
            "https://example.test/",
            {"YESCAPTCHA_CLIENT_KEY": "test-client-key"},
        )

        self.assertEqual(token, "turnstile-token")
        for call in request_with_retry.call_args_list:
            self.assertIs(
                call.args[0].__self__,
                signup._CAPTCHA_SERVICE_SESSION,
            )
        self.assertFalse(signup._CAPTCHA_SERVICE_SESSION.trust_env)
        self.assertEqual(signup._CAPTCHA_SERVICE_SESSION.proxies, {})
        create_payload = request_with_retry.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["softID"], "102154")
        self.assertNotIn("softID", create_payload["task"])

    @patch("signup.svg_to_png_base64", return_value="png-base64")
    @patch("signup.time.sleep", return_value=None)
    @patch("signup.external_request_with_retry")
    def test_image_create_task_includes_developer_soft_id(
        self,
        request_with_retry,
        _sleep,
        _convert,
    ):
        request_with_retry.side_effect = [
            FakeJsonResponse({"errorId": 0, "taskId": "image-task"}),
            FakeJsonResponse(
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"text": "AB12"},
                }
            ),
        ]

        result = signup.recognize_captcha_with_yescaptcha(
            "svg-base64",
            {"YESCAPTCHA_CLIENT_KEY": "test-client-key"},
        )

        self.assertEqual(result, "AB12")
        for call in request_with_retry.call_args_list:
            self.assertIs(
                call.args[0].__self__,
                signup._CAPTCHA_SERVICE_SESSION,
            )
        create_payload = request_with_retry.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["softID"], "102154")
        self.assertNotIn("softID", create_payload["task"])


class VerificationSession:
    def __init__(self):
        self.redirect_attempts = 0

    def get(self, url, **kwargs):
        if "email-verification" in url:
            return FakeResponse(
                200,
                url,
                text=(
                    '<form method="post" action="/u/email-verification">'
                    '<input type="hidden" name="state" value="STATE">'
                    '<button name="action" value="confirm">confirm</button>'
                    "</form>"
                ),
            )

        self.redirect_attempts += 1
        if self.redirect_attempts == 1:
            # curl_cffi 的异常不属于 requests.exceptions.RequestException。
            raise RuntimeError(
                "BoringSSL SSL_connect: Connection closed abruptly "
                "(SSL_ERROR_SYSCALL)"
            )
        return FakeResponse(200, "https://app.tavily.com/home")

    def post(self, url, **kwargs):
        return FakeResponse(
            302,
            url,
            location="https://tavily.us.auth0.com/continue",
        )


class VerifyEmailTests(unittest.TestCase):
    @patch("signup.time.sleep", return_value=None)
    def test_retries_transient_tls_error_on_redirect(self, _sleep):
        session = VerificationSession()
        session = signup.RetrySession(
            session, max_attempts=3, retry_delay=0
        )

        result = signup.verify_email(
            session,
            "https://auth.tavily.com/u/email-verification?ticket=TICKET",
            request_max_attempts=3,
            request_retry_delay=0,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["final_url"], "https://app.tavily.com/home")
        self.assertEqual(session.redirect_attempts, 2)

    @patch("signup.time.sleep", return_value=None)
    def test_returns_failure_only_after_all_network_retries(self, _sleep):
        class BrokenSession:
            attempts = 0

            def get(self, url, **kwargs):
                self.attempts += 1
                raise RuntimeError("temporary TLS failure")

        session = BrokenSession()
        session = signup.RetrySession(
            session, max_attempts=3, retry_delay=0
        )
        result = signup.verify_email(
            session,
            "https://auth.tavily.com/u/email-verification?ticket=TICKET",
            request_max_attempts=3,
            request_retry_delay=0,
        )

        self.assertFalse(result["success"])
        self.assertIn("连续 3 次网络请求失败", result["error"])
        self.assertEqual(session._session.attempts, 3)


class RetryPolicyTests(unittest.TestCase):
    def test_network_failures_accumulate_across_nodes_and_rotate(self):
        class Manager:
            proxy_api_url = "http://proxy-api"
            max_network_failures_per_ip = 3
            failures = 0
            rotation_requested = False

            def record_network_failure(self, node, error):
                self.failures += 1
                return self.failures >= 3

            def request_rotation(self, reason):
                self.rotation_requested = True

        class FlakySession:
            attempts = {}

            def get(self, url, **kwargs):
                count = self.attempts.get(url, 0) + 1
                self.attempts[url] = count
                if url.endswith("node-a") and count == 1:
                    raise RuntimeError("TLS down on A")
                if url.endswith("node-b") and count == 1:
                    raise RuntimeError("TLS down on B")
                if url.endswith("node-c"):
                    raise RuntimeError("TLS down on C")
                return FakeResponse(200, url)

        manager = Manager()
        session = signup.RetrySession(
            FlakySession(), proxy_manager=manager, max_attempts=3, retry_delay=0
        )

        self.assertEqual(session.get("https://auth.tavily.com/node-a").status_code, 200)
        self.assertEqual(session.get("https://auth.tavily.com/node-b").status_code, 200)
        with self.assertRaises(signup.ProxyRotationRequired):
            session.get("https://auth.tavily.com/node-c")
        self.assertEqual(manager.failures, 3)
        self.assertTrue(manager.rotation_requested)

    def test_proxy_manager_rotates_and_resets_ip_counters(self):
        manager = ProxyManager(
            proxy_api_url="http://proxy-api",
            max_network_failures_per_ip=3,
            poll_interval=0,
        )
        manager.current_proxy_url = "http://old-proxy:80"
        manager.last_proxy_ip = "192.0.2.10"
        manager.attempts_on_current_ip = 2

        self.assertFalse(manager.record_network_failure("node-a", RuntimeError("a")))
        self.assertFalse(manager.record_network_failure("node-b", RuntimeError("b")))
        self.assertTrue(manager.record_network_failure("node-c", RuntimeError("c")))
        manager.request_rotation("累计网络失败达到 3 次")

        with patch.object(
            manager,
            "fetch_proxy_from_api",
            return_value=("http://new-proxy:80", "198.51.100.20"),
        ), patch.object(
            manager, "detect_exit_ip", return_value="198.51.100.20"
        ):
            new_proxy = manager.get_proxy()

        self.assertEqual(new_proxy, "http://new-proxy:80")
        self.assertEqual(manager.last_proxy_ip, "198.51.100.20")
        self.assertEqual(manager.network_failures_on_current_ip, 0)
        self.assertEqual(manager.attempts_on_current_ip, 0)
        self.assertFalse(manager.rotation_requested)


class BatchResumeTests(unittest.TestCase):
    def test_rotation_reuses_email_and_password(self):
        class Provider:
            email = None

            def acquire_email(self):
                self.email = "same@example.com"
                return self.email

            def close(self):
                pass

        class Manager:
            proxy_api_url = "http://proxy-api"

            def __init__(self):
                self.proxies = iter(["http://proxy-a", "http://proxy-b"])
                self.attempts = 0

            def get_proxy(self):
                return next(self.proxies)

            def record_attempt(self):
                self.attempts += 1

            def force_rotate(self, reason):
                pass

        manager = Manager()
        signup_results = [
            signup.ProxyRotationRequired("IP 累计网络失败达到 3 次"),
            {
                "success": True,
                "api_keys": [{"key": "tvly-test-key"}],
                "session": None,
            },
        ]

        with TemporaryDirectory() as tmpdir, patch(
            "main.ProxyManager", return_value=manager
        ), patch("main.load_config", return_value={}), patch(
            "main.create_mail_provider", return_value=Provider()
        ) as create_provider, patch(
            "main.signup", side_effect=signup_results
        ) as do_signup, patch("main.time.sleep", return_value=None):
            main.batch_signup(
                count=1,
                output_file=f"{tmpdir}/keys.txt",
                failed_file=f"{tmpdir}/failed.txt",
                run_log_file=f"{tmpdir}/run.log",
                password="fixed-password",
                proxy_api_url="http://proxy-api",
            )

        self.assertEqual(create_provider.call_count, 1)
        self.assertEqual(do_signup.call_count, 2)
        first_call, second_call = do_signup.call_args_list
        self.assertEqual(first_call.kwargs["email"], "same@example.com")
        self.assertEqual(second_call.kwargs["email"], "same@example.com")
        self.assertEqual(first_call.kwargs["password"], "fixed-password")
        self.assertEqual(second_call.kwargs["password"], "fixed-password")
        self.assertEqual(first_call.kwargs["proxy"], "http://proxy-a")
        self.assertEqual(second_call.kwargs["proxy"], "http://proxy-b")


if __name__ == "__main__":
    unittest.main()
