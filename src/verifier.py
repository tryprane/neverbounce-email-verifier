import asyncio
import json
import logging
import os
import pathlib
import re
import time
from typing import Any, Dict, List, Optional
from scrapling.fetchers import AsyncStealthySession

logger = logging.getLogger(__name__)

# Pre-cached static PerimeterX captcha.js
CAPTCHA_JS_PATH = pathlib.Path(__file__).parent / "assets" / "captcha.js"
CACHED_CAPTCHA_JS: Optional[bytes] = None
if CAPTCHA_JS_PATH.exists():
    try:
        CACHED_CAPTCHA_JS = CAPTCHA_JS_PATH.read_bytes()
        logger.info("Loaded cached PerimeterX captcha.js (%d bytes).", len(CACHED_CAPTCHA_JS))
    except Exception as e:
        logger.warning("Failed to load cached captcha.js: %s", e)

NEVERBOUNCE_HOME = "https://www.neverbounce.com/"
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

BLOCKED_DOMAINS = {
    "zoominfo.com",
    "googleads.g.doubleclick.net",
    "facebook.com",
    "datadoghq.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "ada.support",
    "clarity.ms",
    "hubspot.com",
    "analytics.google.com",
    "googletagmanager.com",
    "connect.facebook.net",
    "bat.bing.com",
}


def validate_email(email: str) -> bool:
    """Check basic email formatting."""
    if not email or not isinstance(email, str):
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


def parse_flags(flags_list: List[str]) -> Dict[str, bool]:
    """Parse NeverBounce's verification flags into structured booleans."""
    flags_set = set(flags_list or [])
    return {
        "free_email": "free_email_host" in flags_set,
        "role_account": "role_account" in flags_set,
        "smtp_connectable": "smtp_connectable" in flags_set,
        "has_dns": "has_dns" in flags_set,
        "has_dns_mx": "has_dns_mx" in flags_set,
        "historical_response": "historical_response" in flags_set,
    }


async def verify_email_in_page_async(
    email: str,
    proxy_url: Optional[str] = None,
    timeout_ms: int = 28000,
) -> Dict[str, Any]:
    """
    Executes an IP-consistent NeverBounce deliverability verification natively
    using Scrapling's AsyncStealthySession with pre-navigation domain blocking and asset aborting.
    """
    clean_email = email.strip()
    result_holder: Dict[str, Any] = {
        "email": clean_email,
        "success": False,
        "status": "unknown",
        "flags": [],
        "latency_seconds": 0.0,
        "transfer_bytes": 0,
        "method": "async_stealth_in_page",
        "error": None,
    }

    t0 = time.time()
    extracted_data: Dict[str, Any] = {}
    wire_bytes = 0

    session_kwargs: Dict[str, Any] = {
        "headless": True,
        "disable_resources": True,
    }
    if proxy_url:
        session_kwargs["proxy"] = proxy_url

    try:
        async with AsyncStealthySession(**session_kwargs) as session:
            async def on_page(page):
                nonlocal wire_bytes

                async def on_resp(resp):
                    nonlocal wire_bytes
                    try:
                        b = await resp.body()
                        wire_bytes += len(b)
                    except Exception:
                        pass

                page.on("response", on_resp)

                async def route_handler(route):
                    url = route.request.url.lower()
                    if "captcha.js" in url and CACHED_CAPTCHA_JS:
                        await route.fulfill(
                            status=200,
                            content_type="application/javascript",
                            body=CACHED_CAPTCHA_JS,
                            headers={"Access-Control-Allow-Origin": "*"},
                        )
                        return

                    if route.request.resource_type in ["image", "media", "font", "stylesheet"] or any(
                        t in url for t in BLOCKED_DOMAINS
                    ):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", route_handler)

                # In-page script: waits up to 3.5s for PerimeterX _pxhd cookie before firing fetch
                js_script = f"""
                async () => {{
                    try {{
                        const start = Date.now();
                        while (!document.cookie.includes('_pxhd') && (Date.now() - start < 3500)) {{
                            await new Promise(r => setTimeout(r, 150));
                        }}
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
                        return {{ status_code: status, body: text }};
                    }} catch (err) {{
                        return {{ status_code: 0, body: String(err) }};
                    }}
                }}
                """
                eval_res = await page.evaluate(js_script)
                extracted_data.update(eval_res)

            await session.fetch(
                NEVERBOUNCE_HOME,
                page_action=on_page,
                timeout=timeout_ms,
                disable_resources=True,
                blocked_domains=BLOCKED_DOMAINS,
            )

        status_code = extracted_data.get("status_code", 0)
        raw_body = extracted_data.get("body", "")

        if status_code == 200:
            data = json.loads(raw_body)
            st = str(data.get("status", "unknown")).lower()
            flags = data.get("flags", [])
            result_holder["success"] = True
            result_holder["status"] = st
            result_holder["flags"] = flags
            result_holder["error"] = None
        elif status_code == 429:
            result_holder["error"] = "RATE_LIMITED_429"
        elif status_code == 403:
            result_holder["error"] = "BOT_CHALLENGE_403"
        else:
            err_detail = extracted_data.get("error") or f"HTTP_{status_code}: {raw_body[:60]}"
            result_holder["error"] = err_detail

    except Exception as fetch_err:
        result_holder["error"] = f"FetchException: {fetch_err}"

    result_holder["transfer_bytes"] = wire_bytes
    result_holder["latency_seconds"] = round(time.time() - t0, 2)
    return result_holder


def verify_email_in_page_sync(
    email: str,
    proxy_url: Optional[str] = None,
    timeout_ms: int = 28000,
) -> Dict[str, Any]:
    """Synchronous bridge if called from synchronous contexts."""
    return asyncio.run(verify_email_in_page_async(email, proxy_url=proxy_url, timeout_ms=timeout_ms))


class NeverbounceVerifier:
    """Compatibility wrapper for NeverBounce verification."""

    def __init__(self, proxy_url: Optional[str] = None, timeout_seconds: int = 25):
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds

    def verify(self, email: str, allow_stealth_fallback: bool = True) -> Dict[str, Any]:
        res = verify_email_in_page_sync(
            email=email,
            proxy_url=self.proxy_url,
            timeout_ms=self.timeout_seconds * 1000,
        )
        return {
            "success": res.get("success", False),
            "data": {
                "status": res.get("status"),
                "flags": res.get("flags", []),
            },
            "transfer_bytes": res.get("transfer_bytes", 0),
            "method": res.get("method", "async_stealth_in_page"),
            "error": res.get("error"),
        }


class SessionManager:
    """Compatibility stub for session manager."""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds

    def is_valid(self) -> bool:
        return True

    def refresh_session(self) -> bool:
        return True


global_session = SessionManager()
