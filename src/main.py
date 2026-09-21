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
    verify_emails_in_session_async,
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


async def verify_batch_with_retries(
    batch_emails: List[str],
    batch_offset: int,
    total_emails: int,
    proxy_rotator: ProxyRotator,
    max_retries: int = 3,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> List[Dict[str, Any]]:
    """
    Verifies a batch of emails inside an authenticated stealth browser session.
    Automatically rotates proxy session and retries any rate-limited or aborted emails.
    """
    async with (semaphore or asyncio.Lock()):
        pending_emails = list(batch_emails)
        resolved_results: Dict[str, Dict[str, Any]] = {}
        last_session = "none"

        for attempt in range(1, max_retries + 1):
            if not pending_emails:
                break

            proxy_url, session_id = await proxy_rotator.get_proxy_for_attempt(pending_emails[0], attempt)
            last_session = session_id

            try:
                batch_res = await verify_emails_in_session_async(
                    emails=pending_emails,
                    proxy_url=proxy_url,
                    timeout_ms=35000 + (len(pending_emails) * 3000),
                )

                still_unresolved = []
                for item in batch_res:
                    em = item["email"]
                    if item.get("success"):
                        st = item.get("status", "unknown").lower()
                        flags = item.get("flags", [])
                        parsed = parse_flags(flags)
                        rec = {
                            "email": em,
                            "status": st,
                            "is_valid": st == "valid",
                            "flags": flags,
                            "free_email": parsed["free_email"],
                            "role_account": parsed["role_account"],
                            "smtp_connectable": parsed["smtp_connectable"],
                            "has_dns": parsed["has_dns"],
                            "has_dns_mx": parsed["has_dns_mx"],
                            "historical_response": parsed["historical_response"],
                            "verification_method": "stealth_session_batch",
                            "transfer_bytes": item.get("transfer_bytes", 0),
                            "proxy_session": session_id,
                            "attempts": attempt,
                            "latency_seconds": item.get("latency_seconds", 0.0),
                            "error": None,
                        }
                        resolved_results[em] = rec
                        idx_num = batch_offset + batch_emails.index(em) + 1
                        Actor.log.info(
                            f"[{idx_num:03d}/{total_emails:03d}] {em:<34} -> {st.upper():<9} "
                            f"({item.get('latency_seconds')}s, session: {session_id})"
                        )
                        await Actor.push_data(rec)
                    else:
                        err = item.get("error")
                        if err in ("RATE_LIMITED_429", "BOT_CHALLENGE_403", "SESSION_ABORTED") or "FetchException" in str(err):
                            still_unresolved.append(em)
                        else:
                            rec = {
                                "email": em,
                                "status": "unknown",
                                "is_valid": False,
                                "flags": [],
                                "free_email": False,
                                "role_account": False,
                                "smtp_connectable": False,
                                "has_dns": False,
                                "has_dns_mx": False,
                                "historical_response": False,
                                "verification_method": "stealth_session_batch",
                                "transfer_bytes": item.get("transfer_bytes", 0),
                                "proxy_session": session_id,
                                "attempts": attempt,
                                "latency_seconds": item.get("latency_seconds", 0.0),
                                "error": err or "Verification error",
                            }
                            resolved_results[em] = rec
                            idx_num = batch_offset + batch_emails.index(em) + 1
                            Actor.log.warning(
                                f"[{idx_num:03d}/{total_emails:03d}] {em:<34} -> UNKNOWN ({err})"
                            )
                            await Actor.push_data(rec)

                pending_emails = still_unresolved
                if pending_emails and attempt < max_retries:
                    Actor.log.warning(
                        f"Rotating proxy session for {len(pending_emails)} remaining emails in batch (attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(1.0)

            except Exception as e:
                Actor.log.warning(f"Batch attempt {attempt} error: {e}. Rotating proxy...")
                if attempt < max_retries:
                    await asyncio.sleep(1.0)

        # For any emails that exhausted all retries
        for em in pending_emails:
            rec = {
                "email": em,
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
                "proxy_session": last_session,
                "attempts": max_retries,
                "latency_seconds": 0.0,
                "error": "Max retries exceeded",
            }
            resolved_results[em] = rec
            idx_num = batch_offset + batch_emails.index(em) + 1
            Actor.log.error(f"[{idx_num:03d}/{total_emails:03d}] {em:<34} -> FAILED (Max retries exceeded)")
            await Actor.push_data(rec)

        return [resolved_results[em] for em in batch_emails if em in resolved_results]


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

        # 2. Concurrency, Batch size & Retry bounds
        concurrency = int(actor_input.get("max_concurrency") or 1)
        concurrency = max(1, min(10, concurrency))
        max_retries = int(actor_input.get("max_retries") or 3)
        max_retries = max(1, min(5, max_retries))
        batch_size = int(actor_input.get("batch_size") or 5)
        batch_size = max(1, min(10, batch_size))

        # 3. Setup Proxy Rotator
        apify_proxy_config = None
        proxy_input = actor_input.get("proxyConfiguration")

        custom_proxies: List[str] = []
        if actor_input.get("custom_proxies") and isinstance(actor_input.get("custom_proxies"), list):
            custom_proxies.extend(actor_input.get("custom_proxies"))
        if actor_input.get("proxy_url") and isinstance(actor_input.get("proxy_url"), str):
            custom_proxies.append(actor_input.get("proxy_url"))

        if not custom_proxies:
            try:
                if proxy_input:
                    apify_proxy_config = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
                else:
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
            f"Configuration: Concurrency={concurrency} workers | Session Batch Size={batch_size} | "
            f"Retries={max_retries} | Proxy Mode={proxy_type}"
        )

        # 4. Partition emails into session batches
        chunks = [emails[i : i + batch_size] for i in range(0, len(emails), batch_size)]
        semaphore = asyncio.Semaphore(concurrency)
        start_run = time.time()
        results: List[Dict[str, Any]] = []

        async def batch_worker(chunk: List[str], chunk_idx: int):
            offset = chunk_idx * batch_size
            chunk_results = await verify_batch_with_retries(
                batch_emails=chunk,
                batch_offset=offset,
                total_emails=len(emails),
                proxy_rotator=proxy_rotator,
                max_retries=max_retries,
                semaphore=semaphore,
            )
            results.extend(chunk_results)

        tasks = [batch_worker(chunk, c_idx) for c_idx, chunk in enumerate(chunks)]
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

        await Actor.set_value("OUTPUT", summary)

        Actor.log.info("=" * 60)
        Actor.log.info("   NEVERBOUNCE VERIFICATION RUN COMPLETED   ")
        Actor.log.info("=" * 60)
        Actor.log.info(f"Total Processed:       {len(results)}")
        Actor.log.info(f"Valid (Deliverable):   {valid_count} ({round(valid_count/len(results)*100, 1) if results else 0}%)")
        Actor.log.info(f"Catch-all:             {catchall_count} ({round(catchall_count/len(results)*100, 1) if results else 0}%)")
        Actor.log.info(f"Unknown (Protected):   {unknown_count} ({round(unknown_count/len(results)*100, 1) if results else 0}%)")
        Actor.log.info(f"Failed / Timeout:      {failed_count} ({round(failed_count/len(results)*100, 1) if results else 0}%)")
        Actor.log.info(f"Total Bandwidth:       {total_mb} MB (~{avg_bandwidth_kb} KB/email)")
        Actor.log.info(f"Total Run Time:        {total_time}s ({round(total_time/60, 2)} min)")
        Actor.log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
