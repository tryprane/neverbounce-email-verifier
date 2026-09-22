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
    verify_emails_batch_async,
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
    Rotates proxy sessions dynamically across batch calls.
    Supports:
    - Apify Proxy Configuration (Residential / Datacenter / Auto) with unique session IDs
    - Custom Proxies list with round-robin rotation
    - Direct connection fallback
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

    async def get_proxy_for_attempt(self, batch_key: str, attempt: int) -> Tuple[Optional[str], str]:
        """
        Returns (proxy_url, session_identifier).
        Allocates a fresh proxy session on each attempt.
        """
        session_id = f"nb_{secrets.token_hex(4)}_{attempt}"

        # 1. Custom proxies provided by user (e.g. Webshare rotating)
        if self._custom_cycle:
            async with self._lock:
                proxy_url = next(self._custom_cycle)
            label = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
            return proxy_url, f"custom_{label}"

        # 2. Apify Proxy Configuration (Residential or Shared)
        if self.apify_proxy_config:
            try:
                proxy_url = await self.apify_proxy_config.new_url(session_id=session_id)
                return proxy_url, session_id
            except Exception as e:
                Actor.log.warning(f"Error getting Apify proxy URL for session {session_id}: {e}")

        # 3. Direct connection
        return None, "direct"


def format_record(result: Dict[str, Any], session_id: str, attempt: int) -> Dict[str, Any]:
    st = result.get("status", "unknown").lower()
    flags = result.get("flags", [])
    parsed_flags = parse_flags(flags)
    return {
        "email": result.get("email"),
        "status": st,
        "is_valid": st == "valid",
        "flags": flags,
        "free_email": parsed_flags["free_email"],
        "role_account": parsed_flags["role_account"],
        "smtp_connectable": parsed_flags["smtp_connectable"],
        "has_dns": parsed_flags["has_dns"],
        "has_dns_mx": parsed_flags["has_dns_mx"],
        "historical_response": parsed_flags["historical_response"],
        "verification_method": "in_page_batch",
        "transfer_bytes": result.get("transfer_bytes", 550),
        "proxy_session": session_id,
        "attempts": attempt,
        "latency_seconds": result.get("latency_seconds", 0.0),
        "error": result.get("error"),
    }


async def verify_batch_with_retries(
    batch_emails: List[str],
    batch_idx: int,
    total_batches: int,
    proxy_rotator: ProxyRotator,
    max_retries: int = 2,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> List[Dict[str, Any]]:
    """
    Verifies a batch of up to 3 emails in a single browser session.
    Automatically retries any failed emails with a fresh proxy session.
    """
    async with (semaphore or asyncio.Lock()):
        pending = list(batch_emails)
        resolved_records: List[Dict[str, Any]] = []

        for attempt in range(1, max_retries + 1):
            if not pending:
                break

            batch_key = f"b{batch_idx}_{len(pending)}"
            proxy_url, session_id = await proxy_rotator.get_proxy_for_attempt(batch_key, attempt)

            try:
                results = await verify_emails_batch_async(
                    emails=pending,
                    proxy_url=proxy_url,
                    timeout_ms=35000,
                )

                still_pending = []
                for res in results:
                    em = res.get("email")
                    if res.get("success"):
                        record = format_record(res, session_id, attempt)
                        resolved_records.append(record)
                        st = record["status"]
                        lat = record["latency_seconds"]
                        Actor.log.info(f"[{em:<34}] -> {st.upper():<9} ({lat}s, session: {session_id})")
                    else:
                        err = res.get("error")
                        if attempt < max_retries and err in ("RATE_LIMITED_429", "BOT_CHALLENGE_403"):
                            still_pending.append(em)
                            Actor.log.warning(f"[{em}] hit {err} on attempt {attempt}/{max_retries}. Will retry with new session.")
                        else:
                            # Terminal failure or max retries reached
                            record = format_record(res, session_id, attempt)
                            resolved_records.append(record)
                            Actor.log.error(f"[{em}] failed: {err}")

                pending = still_pending

            except Exception as e:
                Actor.log.warning(f"Batch {batch_idx} attempt {attempt} failed: {e}. Rotating proxy...")
                if attempt == max_retries:
                    for em in pending:
                        resolved_records.append({
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
                            "proxy_session": session_id,
                            "attempts": attempt,
                            "latency_seconds": 0.0,
                            "error": f"BatchException: {e}",
                        })
                    pending = []

            if pending and attempt < max_retries:
                await asyncio.sleep(1.0)

        return resolved_records


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

        # 2. Concurrency & Batch Settings
        # Batch size of 3 emails per session is optimal: guarantees staying under Cloudflare's 3/hr per IP limit
        batch_size = int(actor_input.get("batch_size") or 3)
        batch_size = max(1, min(5, batch_size))

        concurrency = int(actor_input.get("max_concurrency") or 1)
        concurrency = max(1, min(10, concurrency))
        max_retries = int(actor_input.get("max_retries") or 2)
        max_retries = max(1, min(5, max_retries))

        # 3. Setup Proxy Rotator
        apify_proxy_config = None
        proxy_input = actor_input.get("proxyConfiguration")

        custom_proxies: List[str] = []
        if actor_input.get("custom_proxies") and isinstance(actor_input.get("custom_proxies"), list):
            custom_proxies.extend(actor_input.get("custom_proxies"))
        if actor_input.get("proxy_url") and isinstance(actor_input.get("proxy_url"), str):
            custom_proxies.append(actor_input.get("proxy_url"))

        # Initialize Apify Proxy if custom proxies not provided
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
            f"Configuration: Concurrency={concurrency} workers | Batch Size={batch_size} emails/session | "
            f"Retries={max_retries} | Proxy Mode={proxy_type}"
        )

        # 4. Chunk emails into batches
        batches = [emails[i : i + batch_size] for i in range(0, len(emails), batch_size)]
        Actor.log.info(f"Split {len(emails)} emails into {len(batches)} in-page session batches.")

        semaphore = asyncio.Semaphore(concurrency)
        start_run = time.time()
        all_results: List[Dict[str, Any]] = []

        async def batch_worker(b_emails: List[str], b_idx: int):
            records = await verify_batch_with_retries(
                batch_emails=b_emails,
                batch_idx=b_idx,
                total_batches=len(batches),
                proxy_rotator=proxy_rotator,
                max_retries=max_retries,
                semaphore=semaphore,
            )
            for rec in records:
                await Actor.push_data(rec)
                all_results.append(rec)

        tasks = [batch_worker(b, i) for i, b in enumerate(batches, 1)]
        await asyncio.gather(*tasks)

        total_time = round(time.time() - start_run, 2)

        # 5. Summary metrics
        valid_count = sum(1 for r in all_results if r["status"] == "valid")
        invalid_count = sum(1 for r in all_results if r["status"] == "invalid")
        catchall_count = sum(1 for r in all_results if r["status"] == "catchall")
        unknown_count = sum(1 for r in all_results if r["status"] == "unknown" and r.get("error") is None)
        failed_count = sum(1 for r in all_results if r.get("error") is not None)

        definitive_count = valid_count + invalid_count + catchall_count
        success_rate = round((definitive_count / len(all_results)) * 100, 1) if all_results else 0.0
        total_transfer_bytes = sum(r.get("transfer_bytes", 550) for r in all_results)
        total_mb = round(total_transfer_bytes / (1024 * 1024), 3)
        avg_bandwidth_kb = round((total_transfer_bytes / 1024) / len(all_results), 2) if all_results else 0.0

        summary = {
            "total_emails": len(all_results),
            "valid_count": valid_count,
            "invalid_count": invalid_count,
            "catchall_count": catchall_count,
            "unknown_count": unknown_count,
            "failed_count": failed_count,
            "success_rate_percent": success_rate,
            "total_bandwidth_mb": total_mb,
            "avg_bandwidth_per_email_kb": avg_bandwidth_kb,
            "duration_seconds": total_time,
            "avg_latency_seconds": round(total_time / len(all_results), 2) if all_results else 0.0,
        }

        await Actor.set_value("OUTPUT", summary)

        Actor.log.info("=" * 60)
        Actor.log.info("   NEVERBOUNCE VERIFICATION RUN COMPLETED   ")
        Actor.log.info("=" * 60)
        Actor.log.info(f"Total Processed:       {len(all_results)}")
        Actor.log.info(f"Valid (Deliverable):   {valid_count} ({round(valid_count/len(all_results)*100, 1)}%)")
        Actor.log.info(f"Catch-all:             {catchall_count} ({round(catchall_count/len(all_results)*100, 1)}%)")
        Actor.log.info(f"Unknown (Protected):   {unknown_count} ({round(unknown_count/len(all_results)*100, 1)}%)")
        Actor.log.info(f"Failed / Timeout:      {failed_count} ({round(failed_count/len(all_results)*100, 1)}%)")
        Actor.log.info(f"Total Bandwidth:       {total_mb} MB (~{avg_bandwidth_kb} KB/email)")
        Actor.log.info(f"Total Run Time:        {total_time}s ({round(total_time/60, 2)} min)")
        Actor.log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
