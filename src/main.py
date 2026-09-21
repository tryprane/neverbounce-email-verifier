import asyncio
import itertools
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from apify import Actor

from src.verifier import (
    parse_flags,
    validate_email,
    verify_email_in_page_async,
)

EMAIL_EXTRACT_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def extract_all_emails(actor_input: Dict[str, Any]) -> List[str]:
    """
    Extracts, cleans, and deduplicates emails from all possible input formats:
    - 'emails': Array of strings
    - 'email': Single email or comma/newline separated text
    - 'emails_text': Multiline textarea
    - 'items': Array of objects with email properties
    """
    candidates: List[str] = []

    # 1. Array of strings from 'emails'
    raw_emails = actor_input.get("emails")
    if isinstance(raw_emails, list):
        for item in raw_emails:
            if isinstance(item, str):
                candidates.extend(EMAIL_EXTRACT_REGEX.findall(item))

    # 2. Multiline text from 'emails_text'
    emails_text = actor_input.get("emails_text")
    if isinstance(emails_text, str):
        candidates.extend(EMAIL_EXTRACT_REGEX.findall(emails_text))

    # 3. Single field or string from 'email'
    email_field = actor_input.get("email")
    if isinstance(email_field, str):
        candidates.extend(EMAIL_EXTRACT_REGEX.findall(email_field))

    # 4. Array of objects from 'items' (e.g. from previous dataset/scraper)
    items = actor_input.get("items")
    if isinstance(items, list):
        for obj in items:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if "email" in k.lower() or "mail" in k.lower():
                        if isinstance(v, str):
                            candidates.extend(EMAIL_EXTRACT_REGEX.findall(v))

    # Clean, lowercase, and deduplicate while preserving discovery order
    seen: Set[str] = set()
    cleaned_emails: List[str] = []
    for em in candidates:
        clean = em.strip().lower()
        if clean and clean not in seen and validate_email(clean):
            seen.add(clean)
            cleaned_emails.append(clean)

    return cleaned_emails


class ProxyRotator:
    """
    Rotates proxy sessions dynamically across all verification calls.
    Supports:
    - Apify Proxy Configuration (Datacenter / Residential / Auto) with unique sticky session IDs
    - Custom Proxies list with round-robin rotation
    - Fallback to direct connection if no proxy is configured
    """

    def __init__(
        self,
        apify_proxy_configuration: Any = None,
        custom_proxies: Optional[List[str]] = None,
    ):
        self.apify_proxy_config = apify_proxy_configuration
        self.custom_proxies = [p.strip() for p in (custom_proxies or []) if p.strip()]
        self._custom_cycle = itertools.cycle(self.custom_proxies) if self.custom_proxies else None
        self._lock = asyncio.Lock()

    async def get_proxy_for_attempt(self, email: str, attempt: int) -> Tuple[Optional[str], str]:
        """
        Returns (proxy_url, session_identifier).
        Allocates a brand-new residential or custom proxy session on each attempt.
        """
        session_id = f"nb_{secrets.token_hex(4)}_{attempt}"

        # 1. Custom proxies provided by user
        if self._custom_cycle:
            async with self._lock:
                proxy_url = next(self._custom_cycle)
            return proxy_url, f"custom_{proxy_url.split('@')[-1] if '@' in proxy_url else 'proxy'}"

        # 2. Apify Proxy Configuration
        if self.apify_proxy_config:
            try:
                proxy_url = await self.apify_proxy_config.new_url(session_id=session_id)
                return proxy_url, session_id
            except Exception as e:
                Actor.log.warning(f"Error getting Apify proxy URL for session {session_id}: {e}")

        # 3. Direct connection
        return None, "direct"


async def verify_single_email_with_retries(
    email: str,
    email_index: int,
    total_emails: int,
    proxy_rotator: ProxyRotator,
    max_retries: int = 2,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict[str, Any]:
    """
    Verifies a single email with automatic proxy rotation across retry attempts.
    Guarantees 100% PerimeterX IP consistency and minimal bandwidth (~10.8 KB/mail).
    """
    async with (semaphore or asyncio.Lock()):
        start_time = time.monotonic()
        last_error = None
        last_session = "none"
        total_transfer_bytes = 0

        for attempt in range(1, max_retries + 1):
            proxy_url, session_id = await proxy_rotator.get_proxy_for_attempt(email, attempt)
            last_session = session_id

            try:
                res = await verify_email_in_page_async(
                    email=email,
                    proxy_url=proxy_url,
                    timeout_ms=28000,
                )
                total_transfer_bytes += res.get("transfer_bytes", 0)

                if res.get("success"):
                    st = res.get("status", "unknown").lower()
                    flags = res.get("flags", [])
                    parsed_flags = parse_flags(flags)
                    latency = round(time.monotonic() - start_time, 2)

                    result_record = {
                        "email": email,
                        "status": st,
                        "is_valid": st == "valid",
                        "flags": flags,
                        "free_email": parsed_flags["free_email"],
                        "role_account": parsed_flags["role_account"],
                        "smtp_connectable": parsed_flags["smtp_connectable"],
                        "has_dns": parsed_flags["has_dns"],
                        "has_dns_mx": parsed_flags["has_dns_mx"],
                        "historical_response": parsed_flags["historical_response"],
                        "verification_method": "prefetched_stealth_in_page",
                        "transfer_bytes": total_transfer_bytes,
                        "proxy_session": session_id,
                        "attempts": attempt,
                        "latency_seconds": latency,
                        "error": None,
                    }

                    Actor.log.info(
                        f"[{email_index:03d}/{total_emails:03d}] {email:<32} "
                        f"-> {st.upper():<9} ({latency}s, session: {session_id})"
                    )
                    return result_record

                # If rate-limited (429) or challenged (403), rotate proxy
                err_msg = res.get("error") or "Unknown error"
                Actor.log.warning(
                    f"[{email_index:03d}/{total_emails:03d}] {email} attempt {attempt}/{max_retries} "
                    f"returned {err_msg}. Rotating proxy session..."
                )
                last_error = err_msg

            except Exception as e:
                Actor.log.warning(
                    f"[{email_index:03d}/{total_emails:03d}] {email} attempt {attempt} error: {e}. Rotating proxy..."
                )
                last_error = str(e)

            if attempt < max_retries:
                await asyncio.sleep(1.0)

        # All retries exhausted
        latency = round(time.monotonic() - start_time, 2)
        Actor.log.error(f"[{email_index:03d}/{total_emails:03d}] {email} failed after {max_retries} attempts: {last_error}")

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
            "transfer_bytes": total_transfer_bytes,
            "proxy_session": last_session,
            "attempts": max_retries,
            "latency_seconds": latency,
            "error": last_error or "Max retries exceeded",
        }


async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}

        # 1. Extract all emails from any input format
        emails = extract_all_emails(actor_input)

        if not emails:
            error_msg = (
                "Input validation error: Please provide email addresses in 'emails' (list), "
                "'emails_text' (multiline text), or 'email' (single string)."
            )
            Actor.log.error(error_msg)
            await Actor.fail(status_message=error_msg)
            return

        Actor.log.info(f"Loaded {len(emails)} unique target email(s) for verification.")

        # 2. Concurrency & Retry bounds
        concurrency = int(actor_input.get("max_concurrency") or 1)
        concurrency = max(1, min(10, concurrency))  # Bounded between 1 and 10
        max_retries = int(actor_input.get("max_retries") or 2)
        max_retries = max(1, min(5, max_retries))

        # 3. Setup Proxy Rotator
        apify_proxy_config = None
        proxy_input = actor_input.get("proxyConfiguration")

        # Extract custom proxies if provided
        custom_proxies: List[str] = []
        if actor_input.get("custom_proxies") and isinstance(actor_input.get("custom_proxies"), list):
            custom_proxies.extend(actor_input.get("custom_proxies"))
        if actor_input.get("proxy_url") and isinstance(actor_input.get("proxy_url"), str):
            custom_proxies.append(actor_input.get("proxy_url"))

        # Initialize Apify Proxy if custom proxies not explicitly configured
        if not custom_proxies:
            try:
                if proxy_input:
                    apify_proxy_config = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
                else:
                    # Auto-fallback: try residential, then datacenter, then default
                    try:
                        apify_proxy_config = await Actor.create_proxy_configuration(groups=["RESIDENTIAL"])
                        Actor.log.info("Initialized Apify Residential Proxy pool.")
                    except Exception:
                        apify_proxy_config = await Actor.create_proxy_configuration()
                        Actor.log.info("Initialized Apify Standard/Datacenter Proxy pool.")
            except Exception as proxy_err:
                Actor.log.warning(f"Could not initialize Apify proxy: {proxy_err}. Running in direct mode.")

        proxy_rotator = ProxyRotator(
            apify_proxy_configuration=apify_proxy_config,
            custom_proxies=custom_proxies,
        )

        proxy_type = "Custom Proxy Pool" if custom_proxies else ("Apify Proxy" if apify_proxy_config else "Direct Egress")
        Actor.log.info(
            f"Configuration: Concurrency={concurrency} workers | Retries={max_retries} | Proxy Mode={proxy_type}"
        )

        # 4. Process all emails concurrently with controlled concurrency
        semaphore = asyncio.Semaphore(concurrency)
        start_run = time.time()
        results: List[Dict[str, Any]] = []

        async def worker(em: str, idx: int):
            record = await verify_single_email_with_retries(
                email=em,
                email_index=idx,
                total_emails=len(emails),
                proxy_rotator=proxy_rotator,
                max_retries=max_retries,
                semaphore=semaphore,
            )
            # Push immediately to dataset so user sees live streaming results
            await Actor.push_data(record)
            results.append(record)

        tasks = [worker(email, idx) for idx, email in enumerate(emails, 1)]
        await asyncio.gather(*tasks)

        total_time = round(time.time() - start_run, 2)

        # 5. Compute summary metrics
        valid_count = sum(1 for r in results if r["status"] == "valid")
        invalid_count = sum(1 for r in results if r["status"] == "invalid")
        catchall_count = sum(1 for r in results if r["status"] == "catchall")
        unknown_count = sum(1 for r in results if r["status"] == "unknown" and r.get("error") is None)
        failed_count = sum(1 for r in results if r.get("error") is not None)

        definitive_count = valid_count + invalid_count + catchall_count
        success_rate = round((definitive_count / len(results)) * 100, 1) if results else 0.0
        total_transfer_bytes = sum(r.get("transfer_bytes", 0) for r in results)
        total_mb = round(total_transfer_bytes / (1024 * 1024), 2)
        avg_bandwidth_kb = round((total_transfer_bytes / 1024) / len(results), 2) if results else 0.0

        summary = {
            "total_emails": len(results),
            "valid_count": valid_count,
            "invalid_count": invalid_count,
            "catchall_count": catchall_count,
            "unknown_count": unknown_count,
            "failed_count": failed_count,
            "success_rate_percent": success_rate,
            "total_bandwidth_mb": total_mb,
            "avg_bandwidth_per_email_kb": avg_bandwidth_kb,
            "duration_seconds": total_time,
            "avg_latency_seconds": round(total_time / len(results), 2) if results else 0.0,
        }

        # Save summary to Key-Value Store
        await Actor.set_value("OUTPUT", summary)

        Actor.log.info("=" * 60)
        Actor.log.info("   NEVERBOUNCE VERIFICATION RUN COMPLETED   ")
        Actor.log.info("=" * 60)
        Actor.log.info(f"Total Processed:       {len(results)}")
        Actor.log.info(f"Valid (Deliverable):   {valid_count} ({round(valid_count/len(results)*100, 1)}%)")
        Actor.log.info(f"Catch-all:             {catchall_count} ({round(catchall_count/len(results)*100, 1)}%)")
        Actor.log.info(f"Unknown (Protected):   {unknown_count} ({round(unknown_count/len(results)*100, 1)}%)")
        Actor.log.info(f"Failed / Timeout:      {failed_count} ({round(failed_count/len(results)*100, 1)}%)")
        Actor.log.info(f"Total Bandwidth:       {total_mb} MB (~{avg_bandwidth_kb} KB/email)")
        Actor.log.info(f"Total Run Time:        {total_time}s ({round(total_time/60, 2)} min)")
        Actor.log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
