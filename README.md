# NeverBounce Bulk Email Verifier Actor

A lightning-fast, ultra-low bandwidth email deliverability verification Actor powered by **NeverBounce** and **Scrapling StealthyFetcher**, with automated **Apify Proxy session rotation**, **multi-input batch parsing**, and **in-page PerimeterX IP consistency**.

---

## ⚡ Key Features

- **Flexible Multi-Input Support**:
  - `emails`: List of email strings (`["a@b.com", "c@d.com"]`).
  - `emails_text`: Paste raw bulk text with emails separated by commas, newlines, or spaces.
  - `email`: Single email address for quick testing.
  - `items`: Pass raw objects from previous scraping runs (`[{"email": "..."}, ...]`).
- **Internal Proxy Rotation & Session Isolation**:
  - Automatically allocates a unique sticky session ID (`session_id = nb_<hex>_<attempt>`) for each email and retry attempt.
  - Seamlessly integrates with **Apify Proxy** (Residential, Datacenter, or Auto).
  - Also supports a list of custom proxy URLs (`custom_proxies`) with round-robin rotation.
- **100% PerimeterX IP Consistency**:
  - Solves the PerimeterX IP mismatch by executing verification inside the browser page context over the identical TLS socket.
  - Eliminates `403 BOT_CHALLENGE` access denials.
- **Minimal Proxy Bandwidth**:
  - Pre-cached PerimeterX `captcha.js` is fulfilled directly from RAM (**0 external proxy bytes**).
  - Aborts images, media, fonts, stylesheets, and ad trackers.
  - Average bandwidth: **only ~10.8 KB per verification**!
- **Concurrent Execution & Live Streaming**:
  - Configurable parallel workers (`max_concurrency`: 1–10).
  - Streams each verified email directly into the Apify dataset in real time.
  - Stores a comprehensive run summary in the default Key-Value store (`OUTPUT`).

---

## 📥 Input Parameters

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `emails` | Array of Strings | `[]` | List of email addresses to verify. |
| `emails_text` | String (Textarea) | `""` | Bulk paste text with emails (separated by commas, newlines, spaces). |
| `email` | String | `""` | Single email address for quick test. |
| `proxyConfiguration` | Object | `{ "useApifyProxy": true }` | Apify Proxy configuration (Automatic, Datacenter, or Residential). |
| `custom_proxies` | Array of Strings | `[]` | *(Optional)* List of custom proxy URLs (`http://user:pass@host:port`) to rotate round-robin. |
| `max_concurrency` | Integer | `3` | Number of concurrent verification workers (1–10). |
| `max_retries` | Integer | `2` | Max retry attempts with fresh rotated proxy session on errors. |

### Example 1: Bulk Paste Text Input
```json
{
  "emails_text": "contact@stripe.com, satya@microsoft.com\nbill.gates@gatesfoundation.org, invalid.fake.123@google.com",
  "max_concurrency": 4
}
```

### Example 2: Structured Array Input
```json
{
  "emails": [
    "satya@microsoft.com",
    "bill.gates@gatesfoundation.org",
    "support@neverbounce.com"
  ],
  "proxyConfiguration": {
    "useApifyProxy": true
  },
  "max_concurrency": 3
}
```

---

## 📤 Output Dataset Format

Each verified email generates a structured record in the default Apify dataset:

```json
{
  "email": "satya@microsoft.com",
  "status": "valid",
  "is_valid": true,
  "historical_response": true,
  "free_email": false,
  "role_account": false,
  "smtp_connectable": true,
  "has_dns": true,
  "has_dns_mx": true,
  "flags": [
    "has_dns",
    "has_dns_mx",
    "smtp_connectable",
    "historical_response"
  ],
  "verification_method": "stealth_in_page",
  "transfer_bytes": 10800,
  "proxy_session": "nb_a1b2_1",
  "attempts": 1,
  "latency_seconds": 8.42,
  "error": null
}
```

---

## 📊 Summary Output (Key-Value Store: `OUTPUT`)

```json
{
  "total_emails": 100,
  "valid_count": 72,
  "invalid_count": 14,
  "catchall_count": 4,
  "unknown_count": 10,
  "failed_count": 0,
  "success_rate_percent": 90.0,
  "total_bandwidth_mb": 1.08,
  "avg_bandwidth_per_email_kb": 10.8,
  "duration_seconds": 245.3,
  "avg_latency_seconds": 2.45
}
```
