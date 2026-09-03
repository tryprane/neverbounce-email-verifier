import asyncio
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from apify import Actor

from src.verifier import NeverbounceVerifier, validate_email, global_session

MAX_RETRIES_PER_EMAIL = 4
RETRY_BACKOFF_SECONDS = 1.2


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


async def verify_email_with_retry(
    email: str,
    proxy_configuration: Any,
    custom_proxy_url: Optional[str],
) -> Dict[str, Any]:
    """
    Verify an email with automatic Apify Residential Proxy rotation and retries.
    Uses ultra-lightweight direct HTTP (~1.5 KB bandwidth) and seamlessly rotates
    proxy sessions on 429 rate limits or timeouts.
    """
    start_time = time.monotonic()
    last_error = None
    last_session_id = None

    for attempt in range(1, MAX_RETRIES_PER_EMAIL + 1):
        # Generate a unique residential proxy session for clean IP allocation
        current_proxy = custom_proxy_url
        if proxy_configuration:
            session_id = f"nb_{secrets.token_hex(4)}_{attempt}"
            last_session_id = session_id
            current_proxy = await proxy_configuration.new_url(session_id=session_id)

        Actor.log.info(f"[{email}] Verification attempt {attempt}/{MAX_RETRIES_PER_EMAIL} (session: {last_session_id or 'default'})...")

        try:
            verifier = NeverbounceVerifier(proxy_url=current_proxy, timeout_seconds=15)
            res = await asyncio.to_thread(verifier.verify, email)

            if res.get("success") and res.get("data"):
                data = res["data"]
                status = str(data.get("status", "unknown")).lower()
                flags = data.get("flags", [])
                parsed_flags = parse_flags(flags)
                latency = round(time.monotonic() - start_time, 2)
                method = res.get("method", "fast_http")
                transfer_bytes = res.get("transfer_bytes", 150)

                Actor.log.info(
                    f"[{email}] Verified successfully in {latency}s via {method} ({transfer_bytes} bytes) -> Status: {status}"
                )
                return {
                    "email": email,
                    "status": status,
                    "is_valid": status == "valid",
                    "flags": flags,
                    "free_email": parsed_flags["free_email"],
                    "role_account": parsed_flags["role_account"],
                    "smtp_connectable": parsed_flags["smtp_connectable"],
                    "has_dns": parsed_flags["has_dns"],
                    "has_dns_mx": parsed_flags["has_dns_mx"],
                    "historical_response": parsed_flags["historical_response"],
                    "verification_method": method,
                    "transfer_bytes": transfer_bytes,
                    "proxy_session": last_session_id,
                    "attempts": attempt,
                    "latency_seconds": latency,
                    "error": None,
                }

            # If error returned (e.g. 429 rate limit or 403 challenge), log warning and rotate session
            err_msg = res.get("error") or f"HTTP {res.get('status_code')}"
            Actor.log.warning(f"[{email}] Attempt {attempt} returned: {err_msg}. Rotating proxy session...")
            last_error = err_msg

        except Exception as e:
            Actor.log.warning(f"[{email}] Attempt {attempt} encountered exception: {e}. Rotating proxy session...")
            last_error = str(e)

        if attempt < MAX_RETRIES_PER_EMAIL:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)

    # If all attempts exhausted, return structured failure record
    latency = round(time.monotonic() - start_time, 2)
    Actor.log.error(f"[{email}] All {MAX_RETRIES_PER_EMAIL} attempts failed. Last error: {last_error}")
    return {
        "email": email,
        "status": "unknown",
        "is_valid": False,
        "flags": [],
        "free_email": False,
        "role_account": False,
        "smtp_connectable": False,
        "has_dns": False,
        "has_dns_mx": False,
        "historical_response": False,
        "verification_method": "failed",
        "transfer_bytes": 0,
        "proxy_session": last_session_id,
        "attempts": MAX_RETRIES_PER_EMAIL,
        "latency_seconds": latency,
        "error": last_error or "Max retries exceeded",
    }


async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}

        # Collect emails from single 'email' or list 'emails'
        raw_emails: List[str] = []
        if actor_input.get("email"):
            raw_emails.append(str(actor_input.get("email")).strip())
        if actor_input.get("emails") and isinstance(actor_input.get("emails"), list):
            for e in actor_input.get("emails"):
                if e and isinstance(e, str):
                    raw_emails.append(e.strip())

        # Deduplicate while preserving order
        unique_emails = list(dict.fromkeys(raw_emails))

        if not unique_emails:
            error_msg = "Input validation error: Please provide at least one email address in 'email' or 'emails'."
            Actor.log.error(error_msg)
            await Actor.push_data({
                "email": "invalid@input.none",
                "status": "invalid",
                "is_valid": False,
                "flags": [],
                "free_email": False,
                "role_account": False,
                "smtp_connectable": False,
                "has_dns": False,
                "has_dns_mx": False,
                "historical_response": False,
                "verification_method": "invalid_input",
                "transfer_bytes": 0,
                "proxy_session": None,
                "attempts": 0,
                "latency_seconds": 0.0,
                "error": error_msg,
            })
            await Actor.fail(status_message=error_msg)
            return

        Actor.log.info(f"Starting NeverBounce verification for {len(unique_emails)} email(s)...")

        # Pre-mint PerimeterX session cookie locally (0 proxy bandwidth)
        Actor.log.info("Initializing authentic browser session...")
        await asyncio.to_thread(global_session.refresh_session)

        # Initialize Apify Residential Proxy Configuration
        proxy_configuration = None
        custom_proxy_url = actor_input.get("proxy_url") or os.environ.get("PROXY_URL")
        try:
            proxy_configuration = await Actor.create_proxy_configuration(groups=["RESIDENTIAL"])
            if proxy_configuration:
                Actor.log.info("Apify Residential Proxy initialized successfully.")
            elif not custom_proxy_url:
                Actor.log.warning("Running without proxy configuration. May face Cloudflare rate limits.")
        except Exception as proxy_init_err:
            Actor.log.warning(f"Could not initialize Apify Residential Proxy: {proxy_init_err}")

        # Process emails sequentially or concurrently with controlled parallelism
        for email in unique_emails:
            if not validate_email(email):
                Actor.log.warning(f"Email '{email}' has invalid syntax. Flagging as invalid.")
                output = {
                    "email": email,
                    "status": "invalid",
                    "is_valid": False,
                    "flags": ["syntax_error"],
                    "free_email": False,
                    "role_account": False,
                    "smtp_connectable": False,
                    "has_dns": False,
                    "has_dns_mx": False,
                    "historical_response": False,
                    "verification_method": "syntax_check",
                    "transfer_bytes": 0,
                    "proxy_session": None,
                    "attempts": 0,
                    "latency_seconds": 0.0,
                    "error": "Invalid email syntax format",
                }
                await Actor.push_data(output)
                continue

            result = await verify_email_with_retry(
                email=email,
                proxy_configuration=proxy_configuration,
                custom_proxy_url=custom_proxy_url,
            )
            await Actor.push_data(result)

        Actor.log.info("NeverBounce verification run completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
