import json
import logging
import re
from typing import Any, Dict, Optional
from scrapling.fetchers import StealthyFetcher

logger = logging.getLogger(__name__)

NEVERBOUNCE_HOME = "https://www.neverbounce.com/"
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def validate_email(email: str) -> bool:
    """Check basic email formatting."""
    if not email or not isinstance(email, str):
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


class NeverbounceVerifier:
    """
    Ultra-stealth email verifier interacting with NeverBounce's real-time API
    via Scrapling's StealthyFetcher and Apify Residential Proxies.
    """

    def __init__(self, proxy_url: Optional[str] = None, timeout_ms: int = 25000):
        self.proxy_url = proxy_url
        self.timeout_ms = timeout_ms

    def verify(self, email: str) -> Dict[str, Any]:
        """
        Verify a single email using NeverBounce's in-page verification engine.
        Bypasses PerimeterX and Cloudflare bot challenges seamlessly.
        """
        clean_email = email.strip()
        result_holder: Dict[str, Any] = {
            "success": False,
            "status_code": 0,
            "data": None,
            "error": None
        }

        def on_page_action(page):
            try:
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
                        let text = '';
                        try {{
                            text = await response.text();
                        }} catch (e) {{
                            text = '';
                        }}

                        return {{
                            status: status,
                            body: text
                        }};
                    }} catch (err) {{
                        return {{
                            status: 0,
                            body: String(err)
                        }};
                    }}
                }}
                """
                res = page.evaluate(js_script)
                result_holder["status_code"] = res.get("status", 0)
                raw_body = res.get("body", "")

                if result_holder["status_code"] == 200:
                    try:
                        parsed = json.loads(raw_body)
                        result_holder["success"] = True
                        result_holder["data"] = parsed
                    except Exception as json_err:
                        result_holder["error"] = f"JSON decode failed: {json_err} (raw: {raw_body[:100]})"
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
            "timeout": self.timeout_ms
        }
        if self.proxy_url:
            fetch_kwargs["proxy"] = self.proxy_url

        try:
            StealthyFetcher.fetch(NEVERBOUNCE_HOME, **fetch_kwargs)
        except Exception as fetch_err:
            result_holder["error"] = str(fetch_err)

        return result_holder
