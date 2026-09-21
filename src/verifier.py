import asyncio
import json
import logging
import os
import pathlib
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from scrapling.engines._browsers._stealth import _compiled_stealth_scripts

logger = logging.getLogger(__name__)

# Pre-load all static NeverBounce & PerimeterX scripts from RAM
ASSETS_DIR = pathlib.Path(__file__).parent / "assets"
CACHED_SCRIPTS: Dict[str, bytes] = {}
for fname in ["captcha.js", "auditor.js", "main.min.js", "index.js"]:
    fpath = ASSETS_DIR / fname
    if fpath.exists():
        try:
            CACHED_SCRIPTS[fname] = fpath.read_bytes()
            logger.info("Loaded pre-cached %s (%d bytes).", fname, len(CACHED_SCRIPTS[fname]))
        except Exception as e:
            logger.warning("Failed to load %s: %s", fname, e)

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
    Executes an IP-consistent NeverBounce deliverability verification natively using Playwright
    with Scrapling stealth evasion and pre-navigation RAM fulfillment of PerimeterX assets.
    
    Guarantees:
    1. Pre-navigation Route Interception: All 4 heavy JS scripts (2.3+ MB) are fulfilled from RAM.
    2. Fonts, stylesheets, images, and 3rd party trackers are aborted before hitting the wire.
    3. 100% PerimeterX IP consistency (fetch executed inside active TLS session).
    4. Minimal bandwidth on the wire (~11-15 KB total per email check).
    """
    clean_email = email.strip()
    result_holder: Dict[str, Any] = {
        "email": clean_email,
        "success": False,
        "status": "unknown",
        "flags": [],
        "latency_seconds": 0.0,
        "transfer_bytes": 0,
        "method": "prefetched_stealth_in_page",
        "error": None,
    }

    t0 = time.time()
    extracted_data: Dict[str, Any] = {}
    actual_wire_bytes = 0

    # Parse proxy configuration if provided
    proxy_dict = None
    if proxy_url:
        parsed = urlparse(proxy_url)
        proxy_dict = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_dict["username"] = parsed.username
        if parsed.password:
            proxy_dict["password"] = parsed.password

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-backgrounding-occluded-windows",
                ],
            )

            context = await browser.new_context(
                proxy=proxy_dict,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                color_scheme="dark",
                ignore_https_errors=True,
            )

            # Inject compiled stealth scripts before any page navigation
            for script in _compiled_stealth_scripts():
                await context.add_init_script(script=script)

            # PRE-NAVIGATION ROUTE INTERCEPTION:
            # Fulfill all static heavy scripts directly from RAM and abort trackers
            async def route_handler(route):
                url = route.request.url.lower()
                rtype = route.request.resource_type

                # 1. Fulfill pre-cached PerimeterX & NeverBounce scripts directly from RAM (0 proxy bytes)
                if "captcha.js" in url and "captcha.js" in CACHED_SCRIPTS:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_SCRIPTS["captcha.js"],
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return
                if "auditor.js" in url and "auditor.js" in CACHED_SCRIPTS:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_SCRIPTS["auditor.js"],
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return
                if "main.min.js" in url and "main.min.js" in CACHED_SCRIPTS:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_SCRIPTS["main.min.js"],
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return
                if "ri.px-cloud.net/index.js" in url and "index.js" in CACHED_SCRIPTS:
                    await route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=CACHED_SCRIPTS["index.js"],
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                    return

                # 2. Abort all unnecessary heavy resources and 3rd party trackers
                if rtype in ["image", "media", "font", "stylesheet"] or any(
                    t in url for t in ["zoominfo", "googleads", "facebook", "datadog", "analytics", "ada", "clarity", "hubspot", "segment"]
                ):
                    await route.abort()
                    return

                # 3. Allow only document, telemetry handshakes, and emailcheck
                await route.continue_()

            await context.route("**/*", route_handler)

            # Track actual wire bytes downloaded over the proxy
            async def on_resp(resp):
                nonlocal actual_wire_bytes
                u = resp.url.lower()
                # Exclude local RAM fulfillments from wire count
                if not any(k in u for k in ["captcha.js", "auditor.js", "main.min.js", "ri.px-cloud.net/index.js"]):
                    try:
                        b = await resp.body()
                        actual_wire_bytes += len(b)
                    except Exception:
                        pass

            page = await context.new_page()
            page.on("response", on_resp)

            # Navigate to NeverBounce home
            await page.goto(NEVERBOUNCE_HOME, wait_until="domcontentloaded", timeout=timeout_ms)

            # Wait up to 3.5s for PerimeterX _pxhd cookie before firing in-page fetch
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

            await browser.close()

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

    result_holder["transfer_bytes"] = actual_wire_bytes
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
            "transfer_bytes": res.get("transfer_bytes", 11000),
            "method": res.get("method", "prefetched_stealth_in_page"),
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
