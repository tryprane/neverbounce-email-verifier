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

# ---------------------------------------------------------------------------
# Pre-cached static assets served from RAM (zero proxy bandwidth)
# ---------------------------------------------------------------------------
CAPTCHA_JS_PATH = pathlib.Path(__file__).parent / "assets" / "captcha.js"
CACHED_CAPTCHA_JS: Optional[bytes] = None
if CAPTCHA_JS_PATH.exists():
    try:
        CACHED_CAPTCHA_JS = CAPTCHA_JS_PATH.read_bytes()
        logger.info("Loaded cached PerimeterX captcha.js (%d bytes).", len(CACHED_CAPTCHA_JS))
    except Exception as e:
        logger.warning("Failed to load cached captcha.js: %s", e)

# PX sensor script cached in RAM after first download (shared across all sessions)
CACHED_PX_SENSOR: Optional[bytes] = None

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


async def verify_email_in_page_async(
    email: str,
    proxy_url: Optional[str] = None,
    timeout_ms: int = 28000,
) -> Dict[str, Any]:
    """
    Bandwidth-optimized NeverBounce email verification.

    Uses a WHITELIST-ONLY routing strategy:
    - Block ALL requests by default (Next.js bundles, CSS, images, trackers → 0 bytes)
    - Only allow: HTML document, PerimeterX sensor scripts, and /api/emailcheck
    - PX captcha.js and px.js are served from RAM cache after first download

    Expected bandwidth: ~50-80 KB per email (down from ~850 KB).
    """
    global CACHED_PX_SENSOR

    clean_email = email.strip()
    result_holder: Dict[str, Any] = {
        "email": clean_email,
        "success": False,
        "status": "unknown",
        "flags": [],
        "latency_seconds": 0.0,
        "transfer_bytes": 0,
        "method": "whitelist_stealth_in_page",
        "error": None,
    }

    t0 = time.time()
    extracted_data: Dict[str, Any] = {}
    wire_bytes = 0  # Only counts bytes that actually went through the proxy

    session_kwargs: Dict[str, Any] = {
        "headless": True,
        "disable_resources": True,
    }
    if proxy_url:
        session_kwargs["proxy"] = proxy_url

    try:
        async with AsyncStealthySession(**session_kwargs) as session:
            context = session.context
            page = await context.new_page()

            # Track only actual wire bytes (not RAM-fulfilled responses)
            ram_fulfilled_urls = set()
            cdp_active = False
            try:
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")

                def on_loading_finished(params):
                    nonlocal wire_bytes
                    wire_bytes += params.get("encodedDataLength", 0)

                cdp.on("Network.loadingFinished", on_loading_finished)
                cdp_active = True
            except Exception:
                pass

            if not cdp_active:
                async def on_resp(resp):
                    nonlocal wire_bytes
                    if resp.url in ram_fulfilled_urls:
                        return
                    try:
                        cl = resp.headers.get("content-length")
                        if cl and cl.isdigit():
                            wire_bytes += int(cl)
                        else:
                            try:
                                b = await resp.body()
                                wire_bytes += len(b)
                            except Exception:
                                pass
                    except Exception:
                        pass

                page.on("response", on_resp)

            # Route handler attached BEFORE page.goto() — blocks 100% of Next.js chunks, GTM, etc.
            async def route_handler(route):
                global CACHED_PX_SENSOR
                url = route.request.url
                url_lower = url.lower()
                rtype = route.request.resource_type

                # 1. Serve captcha.js from RAM (0 bytes wire bandwidth)
                if "captcha.js" in url_lower and CACHED_CAPTCHA_JS:
                    ram_fulfilled_urls.add(url)
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_CAPTCHA_JS,
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return

                # 2. Serve PX sensor from RAM if cached (0 bytes wire bandwidth)
                if "/px.js" in url_lower and CACHED_PX_SENSOR:
                    ram_fulfilled_urls.add(url)
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_PX_SENSOR,
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return

                # 3. Whitelist: Only allow NeverBounce main document, PerimeterX, and emailcheck
                is_main_doc = rtype == "document" and "neverbounce.com" in url_lower
                is_emailcheck = "/api/emailcheck" in url_lower
                is_px_endpoint = any(k in url_lower for k in [
                    "/btfn1q7w/",
                    "/px/",
                    "/b/s/",
                    "collector-px",
                    "client.perimeterx.net",
                    "px-cloud.net",
                    "/px.js",
                ])

                if is_main_doc or is_emailcheck or is_px_endpoint:
                    await route.continue_()
                else:
                    # Instantly abort all Next.js bundles, GTM, OneTrust, images, CSS
                    await route.abort()

            await page.route("**/*", route_handler)

            # After PX sensor loads for the first time, cache it for future sessions
            async def cache_px_sensor(resp):
                global CACHED_PX_SENSOR
                if CACHED_PX_SENSOR is None and "/px.js" in resp.url.lower():
                    try:
                        body = await resp.body()
                        if len(body) > 1000:
                            CACHED_PX_SENSOR = body
                            logger.info("Cached PX sensor script (%d bytes) for future sessions.", len(body))
                    except Exception:
                        pass

            page.on("response", cache_px_sensor)

            # Navigate to NeverBounce home
            await page.goto(NEVERBOUNCE_HOME, wait_until="commit", timeout=timeout_ms)

            # In-page script: waits for PerimeterX _pxhd cookie, then fires emailcheck
            js_script = f"""
            async () => {{
                try {{
                    const start = Date.now();
                    while (!document.cookie.includes('_pxhd') && (Date.now() - start < 6000)) {{
                        await new Promise(r => setTimeout(r, 100));
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

            await asyncio.sleep(0.05)
            await page.close()

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
            "method": res.get("method", "whitelist_stealth_in_page"),
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
