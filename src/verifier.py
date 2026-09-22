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

# Pre-cached static PerimeterX captcha.js to eliminate proxy bandwidth
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


async def verify_emails_batch_async(
    emails: List[str],
    proxy_url: Optional[str] = None,
    timeout_ms: int = 40000,
) -> List[Dict[str, Any]]:
    """
    Verifies a batch of up to 3 emails inside a single authenticated stealth browser session.
    Fulfills PerimeterX captcha.js from RAM, aborts heavy media/trackers, and verifies in-page.
    """
    clean_emails = [e.strip() for e in emails if validate_email(e)]
    if not clean_emails:
        return []

    results: List[Dict[str, Any]] = []
    session_kwargs: Dict[str, Any] = {
        "headless": True,
        "disable_resources": True,
    }
    if proxy_url:
        session_kwargs["proxy"] = proxy_url

    t0 = time.time()
    try:
        async with AsyncStealthySession(**session_kwargs) as session:
            context = session.context
            page = await context.new_page()

            # Pre-navigation route handler attached BEFORE page.goto()
            async def route_handler(route):
                url = route.request.url.lower()
                # Serve captcha.js from RAM (0 external proxy transfer)
                if "captcha.js" in url and CACHED_CAPTCHA_JS:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_CAPTCHA_JS,
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return
                # Abort heavy media, fonts, stylesheets, and ad trackers
                if route.request.resource_type in ["image", "media", "font", "stylesheet"] or any(
                    t in url for t in ["facebook", "googleads", "zoominfo", "datadog", "ada", "analytics"]
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)

            # Fast navigation waiting for domcontentloaded
            await page.goto(NEVERBOUNCE_HOME, wait_until="domcontentloaded", timeout=timeout_ms)

            # Wait for PerimeterX sensor cookie
            for _ in range(40):
                cookie = await page.evaluate("() => document.cookie")
                if "_pxhd" in cookie:
                    break
                await asyncio.sleep(0.1)

            await asyncio.sleep(0.4)

            # In-page batch verification loop inside the active page DOM
            js_batch_script = """
            async (emails) => {
                const out = [];
                for (const em of emails) {
                    try {
                        const t_start = Date.now();
                        const resp = await fetch('/api/emailcheck', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'text/plain;charset=UTF-8',
                                'Origin': 'https://www.neverbounce.com',
                                'Referer': 'https://www.neverbounce.com/'
                            },
                            body: JSON.stringify({ email: em })
                        });
                        const txt = await resp.text();
                        out.push({
                            email: em,
                            status_code: resp.status,
                            body: txt,
                            latency_ms: Date.now() - t_start
                        });
                        if (resp.status === 403 || resp.status === 429) {
                            break;
                        }
                        await new Promise(r => setTimeout(r, 600));
                    } catch (e) {
                        out.push({
                            email: em,
                            status_code: 0,
                            body: String(e),
                            latency_ms: 0
                        });
                    }
                }
                return out;
            }
            """

            batch_eval_results = await page.evaluate(js_batch_script, clean_emails)

            for item in batch_eval_results:
                em = item.get("email")
                sc = item.get("status_code", 0)
                body = item.get("body", "")
                lat = round(item.get("latency_ms", 0) / 1000.0, 2)

                res_dict: Dict[str, Any] = {
                    "email": em,
                    "success": False,
                    "status": "unknown",
                    "flags": [],
                    "latency_seconds": lat,
                    "transfer_bytes": 550,
                    "method": "in_page_stealth_batch",
                    "error": None,
                }

                if sc == 200:
                    try:
                        data = json.loads(body)
                        res_dict["success"] = True
                        res_dict["status"] = str(data.get("status", "unknown")).lower()
                        res_dict["flags"] = data.get("flags", [])
                    except Exception as parse_err:
                        res_dict["error"] = f"JSONDecodeError: {parse_err}"
                elif sc == 429:
                    res_dict["error"] = "RATE_LIMITED_429"
                elif sc == 403:
                    res_dict["error"] = "BOT_CHALLENGE_403"
                else:
                    res_dict["error"] = f"HTTP_{sc}: {body[:60]}"

                results.append(res_dict)

            await page.close()

    except Exception as e:
        logger.warning("Batch session exception: %s", e)
        processed = {r["email"] for r in results}
        for ce in clean_emails:
            if ce not in processed:
                results.append({
                    "email": ce,
                    "success": False,
                    "status": "unknown",
                    "flags": [],
                    "latency_seconds": 0.0,
                    "transfer_bytes": 0,
                    "method": "failed",
                    "error": f"SessionException: {e}",
                })

    return results


async def verify_email_in_page_async(
    email: str,
    proxy_url: Optional[str] = None,
    timeout_ms: int = 28000,
) -> Dict[str, Any]:
    """Single email compatibility wrapper."""
    res_list = await verify_emails_batch_async([email], proxy_url=proxy_url, timeout_ms=timeout_ms)
    if res_list:
        return res_list[0]
    return {
        "email": email,
        "success": False,
        "status": "unknown",
        "flags": [],
        "latency_seconds": 0.0,
        "transfer_bytes": 0,
        "method": "failed",
        "error": "No response returned",
    }


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
            "transfer_bytes": res.get("transfer_bytes", 550),
            "method": res.get("method", "in_page_stealth_batch"),
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
