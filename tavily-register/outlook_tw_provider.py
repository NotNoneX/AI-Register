"""outlook.tw anonymous temporary email provider."""

from __future__ import annotations

import atexit
import threading
import time
from urllib.parse import quote

from curl_cffi import requests

from config import (
    MAX_EMAIL_WAIT_TIME,
    OUTLOOK_TW_BASE_URL,
    OUTLOOK_TW_DOMAIN_INDEX,
    OUTLOOK_TW_POLL_INTERVAL,
    OUTLOOK_TW_REQUEST_RETRIES,
    OUTLOOK_TW_REQUEST_TIMEOUT,
    OUTLOOK_TW_USERNAME_LENGTH,
)
from utils import extract_verification_link


class OutlookTwProviderError(RuntimeError):
    """outlook.tw mailbox creation or polling failed."""


_SHARED_SESSION = None
_SHARED_SESSION_LOCK = threading.RLock()


def _create_http_session():
    # Let curl-cffi keep its internally consistent TLS and browser headers.
    session = requests.Session(impersonate="chrome")
    session.headers.update(
        {
            "Accept": "application/json",
            "Referer": f"{OUTLOOK_TW_BASE_URL}/",
        }
    )
    return session


def _get_shared_session():
    global _SHARED_SESSION
    with _SHARED_SESSION_LOCK:
        if _SHARED_SESSION is None:
            _SHARED_SESSION = _create_http_session()
        return _SHARED_SESSION


def close_shared_outlook_tw_session() -> None:
    global _SHARED_SESSION
    with _SHARED_SESSION_LOCK:
        if _SHARED_SESSION is not None:
            try:
                _SHARED_SESSION.close()
            finally:
                _SHARED_SESSION = None


atexit.register(close_shared_outlook_tw_session)


class OutlookTwProvider:
    def __init__(self, session=None, captcha_solver=None):
        self._uses_shared_session = session is None
        self.session = session or _get_shared_session()
        self._captcha_solver = captcha_solver
        self.email = None
        self.expires_at = None
        self.completed = False
        self.verification_link = None

    @staticmethod
    def _create_session():
        return _create_http_session()

    @staticmethod
    def _captcha_challenge(response) -> tuple[bool, str]:
        if getattr(response, "status_code", None) != 403:
            return False, ""
        try:
            payload = response.json()
        except ValueError:
            return False, ""
        if not isinstance(payload, dict) or payload.get("error") != "captcha-required":
            return False, ""
        return True, str(payload.get("sitekey") or "").strip()

    def _complete_captcha(self, sitekey: str) -> None:
        if self._captcha_solver is None:
            from outlook_tw_captcha import complete_outlook_tw_captcha

            solver = complete_outlook_tw_captcha
        else:
            solver = self._captcha_solver

        # Only one YesCaptcha task may update the process-wide cookie jar at a time.
        with _SHARED_SESSION_LOCK:
            solver(self.session, sitekey)

    def _get_json(self, path, *, params=None):
        last_error = None
        captcha_attempted = False
        attempt = 0
        while attempt < OUTLOOK_TW_REQUEST_RETRIES:
            attempt += 1
            try:
                response = self.session.get(
                    f"{OUTLOOK_TW_BASE_URL}{path}",
                    params=params,
                    timeout=OUTLOOK_TW_REQUEST_TIMEOUT,
                )
                captcha_required, sitekey = self._captcha_challenge(response)
                if captcha_required:
                    if captcha_attempted:
                        from outlook_tw_captcha import OutlookTwCaptchaError

                        raise OutlookTwCaptchaError(
                            "outlook.tw 拒绝了 YesCaptcha token，仍要求 captcha"
                        )
                    self._complete_captcha(sitekey)
                    captcha_attempted = True
                    # Turnstile verification is a prerequisite, not a failed API
                    # attempt; preserve the configured retry budget.
                    attempt -= 1
                    continue
                response.raise_for_status()
                return response.json()
            except OutlookTwProviderError:
                raise
            except (requests.RequestsError, ValueError) as exc:
                last_error = exc
                if attempt < OUTLOOK_TW_REQUEST_RETRIES:
                    time.sleep(min(float(attempt), 2.0))

        raise OutlookTwProviderError(
            f"outlook.tw 请求失败，已重试 {OUTLOOK_TW_REQUEST_RETRIES} 次: {last_error}"
        ) from last_error

    def acquire_email(self) -> str:
        if self.email:
            return self.email

        data = self._get_json(
            "/api/generate",
            params={
                "length": OUTLOOK_TW_USERNAME_LENGTH,
                "domainIndex": OUTLOOK_TW_DOMAIN_INDEX,
            },
        )
        email = str(data.get("email") or "").strip()
        if "@" not in email:
            raise OutlookTwProviderError("outlook.tw 未返回有效邮箱地址")

        self.email = email
        self.expires_at = data.get("expires")
        return email

    def wait_for_verification_link(self) -> str:
        if not self.email:
            raise OutlookTwProviderError("尚未生成 outlook.tw 邮箱")
        if self.verification_link:
            return self.verification_link

        deadline = time.monotonic() + MAX_EMAIL_WAIT_TIME
        last_error = None
        while time.monotonic() < deadline:
            try:
                messages = self._get_json(
                    "/api/emails",
                    params={"mailbox": self.email},
                )
                if not isinstance(messages, list):
                    raise OutlookTwProviderError("outlook.tw 邮件列表格式异常")

                for message in messages:
                    link = self._extract_link_from_message(message)
                    if link:
                        self.completed = True
                        self.verification_link = link
                        return link

                    message_id = message.get("id")
                    if message_id is None:
                        continue
                    detail = self._get_json(
                        f"/api/email/{quote(str(message_id), safe='')}"
                    )
                    link = self._extract_link_from_message(detail)
                    if link:
                        self.completed = True
                        self.verification_link = link
                        return link
                last_error = None
            except (requests.RequestsError, ValueError, OutlookTwProviderError) as exc:
                last_error = exc

            time.sleep(OUTLOOK_TW_POLL_INTERVAL)

        suffix = f": {last_error}" if last_error else ""
        raise OutlookTwProviderError(f"等待 outlook.tw 的 Tavily 验证邮件超时{suffix}")

    @staticmethod
    def _extract_link_from_message(message) -> str | None:
        if not isinstance(message, dict):
            return None
        content = "\n".join(
            str(message.get(field) or "")
            for field in (
                "subject",
                "html_content",
                "content",
                "text_content",
                "preview",
                "verification_code",
            )
        )
        return extract_verification_link(content)

    def cancel(self) -> None:
        # Anonymous outlook.tw mailboxes expire automatically.
        return None

    def close(self) -> None:
        # Production providers share one captcha-bearing HTTP session for the whole
        # process. Explicitly injected sessions retain the historical close behavior.
        if not self._uses_shared_session:
            self.session.close()
