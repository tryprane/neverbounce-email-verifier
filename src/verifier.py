import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from scrapling.fetchers import StealthyFetcher

logger = logging.getLogger(__name__)

NEVERBOUNCE_HOME = "https://www.neverbounce.com/"
NEVERBOUNCE_API = "https://www.neverbounce.com/api/emailcheck"
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def validate_email(email: str) -> bool:
    """Check basic email formatting."""
    if not email or not isinstance(email, str):
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


class SessionManager:
    """
    Manages genuine browser session tokens (_pxhd) obtained locally without proxy
    bandwidth cost, and caches them with expiration.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self.cached_cookie: Optional[str] = None
        self.cached_user_agent: str = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        )
        self.last_solved_at: float = 0.0

    def is_valid(self) -> bool:
        return bool(self.cached_cookie and (time.time() - self.last_solved_at < self.ttl_seconds))

    def refresh_session(self) -> bool:
        """
        Loads NeverBounce home normally (0 proxy data) using Scrapling
        to solve PerimeterX client challenges and obtain authentic cookies.
        """
        logger.info("Minting fresh PerimeterX session cookie (local stealth browser, 0 proxy data)...")
        extracted: Dict[str, str] = {}

        def extract_action(page):
            # Block heavy media and trackers to make local fetch instant (<1.5s)
            try:
                page.route(
                    "**/*",
                    lambda route: (
                        route.abort()
                        if route.request.resource_type in ["image", "media", "font"]
                        or any(t in route.request.url for t in ["facebook", "googleads", "zoominfo", "datadog"])
                        else route.continue_()
                    ),
                )
            except Exception:
                pass

            res = page.evaluate(
                """() => ({
                    cookies: document.cookie,
                    userAgent: navigator.userAgent
                })"""
            )
            extracted.update(res)

        try:
            StealthyFetcher.fetch(NEVERBOUNCE_HOME, page_action=extract_action, headless=True, timeout=25000)
            cookie_str = extracted.get("cookies", "")
            if "_pxhd" in cookie_str:
                self.cached_cookie = cookie_str
                if extracted.get("userAgent"):
                    self.cached_user_agent = extracted["userAgent"]
                self.last_solved_at = time.time()
                logger.info("Successfully acquired PerimeterX session token.")
                return True
        except Exception as e:
            logger.warning("Session token acquisition encountered error: %s", e)

        return False

    def get_headers(self) -> Dict[str, str]:
        if not self.is_valid():
            self.refresh_session()

        headers = {
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://www.neverbounce.com",
            "Referer": "https://www.neverbounce.com/",
            "User-Agent": self.cached_user_agent,
            "Accept": "*/*",
        }
        if self.cached_cookie:
            headers["Cookie"] = self.cached_cookie
        return headers


# Global session manager instance
global_session = SessionManager()


class NeverbounceVerifier:
    """
    Hybrid email verifier:
    - Browser session (_pxhd token) solved locally (0 proxy cost).
    - Email lookup sent via raw HTTP POST through Apify Residential Proxy (only ~1.5 KB per call).
    - Automatic fallback to in-browser stealth execution if proxy requires full JS evaluation.
    """

    def __init__(self, proxy_url: Optional[str] = None, timeout_seconds: int = 15):
        self.proxy_url = proxy_url
        self.timeout = timeout_seconds

    def verify_fast_http(self, email: str) -> Dict[str, Any]:
        """
        Ultra-lightweight direct HTTP request through proxy (~1.5 KB bandwidth).
        """
        clean_email = email.strip()
        headers = global_session.get_headers()
        post_data = json.dumps({"email": clean_email}).encode("utf-8")

        req = urllib.request.Request(NEVERBOUNCE_API, data=post_data, headers=headers)

        if self.proxy_url:
            proxy_handler = urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()

        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status_code = resp.status
                body_bytes = resp.read()
                raw_text = body_bytes.decode("utf-8")

                if status_code == 200:
                    data = json.loads(raw_text)
                    return {
                        "success": True,
                        "status_code": status_code,
                        "data": data,
                        "transfer_bytes": len(post_data) + len(body_bytes),
                        "method": "fast_http",
                        "error": None,
                    }

                return {
                    "success": False,
                    "status_code": status_code,
                    "data": None,
                    "transfer_bytes": len(post_data) + len(body_bytes),
                    "method": "fast_http",
                    "error": f"HTTP_{status_code}",
                }

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            if e.code == 429:
                return {"success": False, "status_code": 429, "data": None, "error": "RATE_LIMITED_429", "method": "fast_http"}
            if e.code == 403:
                return {"success": False, "status_code": 403, "data": None, "error": "BOT_CHALLENGE_403", "method": "fast_http"}
            return {"success": False, "status_code": e.code, "data": None, "error": f"HTTP_{e.code}: {error_body[:100]}", "method": "fast_http"}
        except Exception as e:
            return {"success": False, "status_code": 0, "data": None, "error": str(e), "method": "fast_http"}

    def verify_stealth_fallback(self, email: str) -> Dict[str, Any]:
        """
        Fallback: Full in-page execution via Scrapling if direct HTTP is challenged.
        """
        clean_email = email.strip()
        result_holder: Dict[str, Any] = {
            "success": False,
            "status_code": 0,
            "data": None,
            "transfer_bytes": 0,
            "method": "stealth_fallback",
            "error": None,
        }

        def on_page_action(page):
            try:
                # Abort heavy tracking/images to protect proxy bandwidth
                try:
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in ["image", "media", "font"]
                            or any(t in route.request.url for t in ["facebook", "googleads", "zoominfo"])
                            else route.continue_()
                        ),
                    )
                except Exception:
                    pass

                js_script = f"""
                async () => {{
                    try {{
                        const response = await fetch('/api/emailcheck', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'text/plain;charset=UTF-8',
                                'Origin': 'https://www.neverbounce.com',
                                'Referer': 'https://www.neverbounce.com/'
                            }},
                            body: JSON.stringify({{ email: {json.dumps(clean_email)} }})
                        }});
                        const status = response.status;
                        const text = await response.text();
                        return {{ status: status, body: text }};
                    }} catch (err) {{
                        return {{ status: 0, body: String(err) }};
                    }}
                }}
                """
                res = page.evaluate(js_script)
                result_holder["status_code"] = res.get("status", 0)
                raw_body = res.get("body", "")

                if result_holder["status_code"] == 200:
                    result_holder["data"] = json.loads(raw_body)
                    result_holder["success"] = True
                elif result_holder["status_code"] == 429:
                    result_holder["error"] = "RATE_LIMITED_429"
                elif result_holder["status_code"] == 403:
                    result_holder["error"] = "BOT_CHALLENGE_403"
                else:
                    result_holder["error"] = f"HTTP_{result_holder['status_code']}: {raw_body[:100]}"
            except Exception as eval_err:
                result_holder["error"] = f"Page evaluate error: {eval_err}"

        fetch_kwargs: Dict[str, Any] = {
            "page_action": on_page_action,
            "headless": True,
            "timeout": self.timeout * 1000,
        }
        if self.proxy_url:
            fetch_kwargs["proxy"] = self.proxy_url

        try:
            StealthyFetcher.fetch(NEVERBOUNCE_HOME, **fetch_kwargs)
        except Exception as fetch_err:
            result_holder["error"] = str(fetch_err)

        return result_holder

    def verify(self, email: str) -> Dict[str, Any]:
        """
        Primary verification method:
        1. Attempts fast HTTP (~1.5 KB proxy bandwidth).
        2. Falls back to stealth browser if required.
        """
        # Attempt 1: Fast HTTP with transferred cookie
        fast_res = self.verify_fast_http(email)
        if fast_res.get("success"):
            return fast_res

        # If token was expired or challenged, refresh session and retry once
        if fast_res.get("status_code") == 403:
            global_session.refresh_session()
            fast_retry = self.verify_fast_http(email)
            if fast_retry.get("success"):
                return fast_retry

        # If still failing or rate limited, fallback to stealth browser
        if fast_res.get("error") == "RATE_LIMITED_429":
            return fast_res

        logger.info("Direct HTTP challenge encountered. Engaging stealth browser fallback...")
        return self.verify_stealth_fallback(email)
