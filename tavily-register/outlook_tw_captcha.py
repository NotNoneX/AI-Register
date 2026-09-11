"""YesCaptcha-backed Turnstile bootstrap for outlook.tw."""

from __future__ import annotations

from config import OUTLOOK_TW_BASE_URL, OUTLOOK_TW_REQUEST_TIMEOUT


class OutlookTwCaptchaError(RuntimeError):
    """Raised when outlook.tw's Turnstile challenge cannot be completed."""


def complete_outlook_tw_captcha(session, sitekey: str) -> None:
    """Solve Turnstile with YesCaptcha and attach the resulting session cookie.

    outlook.tw returns the widget sitekey in its ``captcha-required`` response.
    The token is solved for the outlook.tw page and submitted using the same
    request shape as the site's frontend. The supplied HTTP session stores the
    ``mf-captcha`` cookie returned by the server.
    """

    if not sitekey:
        raise OutlookTwCaptchaError("outlook.tw 要求人机验证，但响应中没有 sitekey")

    # Reuse the project's existing YesCaptcha integration and configuration.
    # This import stays local so importing the mail provider does not eagerly
    # initialize signup.py and its optional browser dependencies.
    from signup import load_config, solve_turnstile_with_yescaptcha

    config = load_config() or {}
    token = solve_turnstile_with_yescaptcha(
        sitekey,
        f"{OUTLOOK_TW_BASE_URL}/",
        config,
    )
    if not token:
        raise OutlookTwCaptchaError("YesCaptcha 未返回 outlook.tw Turnstile token")

    try:
        response = session.post(
            f"{OUTLOOK_TW_BASE_URL}/api/captcha",
            headers={
                "Content-Type": "application/json",
                "Origin": OUTLOOK_TW_BASE_URL,
                "cf-turnstile-response": token,
            },
            timeout=OUTLOOK_TW_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        raise OutlookTwCaptchaError(
            f"向 outlook.tw 提交 Turnstile token 失败: {exc}"
        ) from exc

    print("    outlook.tw 人机验证完成，本批次将复用验证会话")
