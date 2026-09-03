# NeverBounce Email Verifier Actor

A lightning-fast, ultra-low bandwidth email deliverability verification Actor powered by **NeverBounce** and **Scrapling StealthyFetcher**, with automated **Apify Residential Proxy rotation**.

## ⚡ What Makes This Architecture Unique?
- **Hybrid Token-Pool Engine**:
  - Solves PerimeterX (`_pxhd`) security tokens using a local stealth browser instance (**0 proxy data consumed**).
  - Routes individual verification checks as lightweight raw HTTP calls through **Apify Residential Proxies** (**only ~1.5 KB per verification** instead of ~600 KB).
  - **Yields up to 500,000+ email verifications per 1 GB of residential proxy data!**
- **Accurate Real-Time Verification**: Retrieves live deliverability status (`valid`, `invalid`, `disposable`, `catch_all`, `unknown`) directly from NeverBounce.
- **Detailed MX, SMTP & History Flags**:
  - `historical_response`: Indicates whether the result was retrieved from NeverBounce's historical database.
  - `smtp_connectable`: Confirms destination mail server accepts incoming connections.
  - `has_dns` & `has_dns_mx`: Verifies active DNS and MX records.
  - `free_email`: Identifies personal webmail (Gmail, Yahoo, Hotmail).
  - `role_account`: Detects generic addresses (admin@, support@, info@).
- **Zero Errors & Automatic Proxy Rotation**: Automatically rotates Apify Residential Proxy sessions on any rate-limiting (`429`) or challenge (`403`), guaranteeing continuous runs without failure.

---

## 📥 Input Parameters

| Field | Type | Description |
| :--- | :--- | :--- |
| `email` | String | Single email address (e.g. `satya@microsoft.com`). |
| `emails` | Array of Strings | Optional list of emails for batch verification. |
| `proxy_url` | String | *(Optional)* Custom residential proxy URL. If left empty, Apify Residential Proxy is used. |

### Example Input
```json
{
  "email": "satya@microsoft.com"
}
```

Or batch:
```json
{
  "emails": [
    "satya@microsoft.com",
    "bill.gates@gatesfoundation.org",
    "support@neverbounce.com"
  ]
}
```

---

## 📤 Output Dataset Format

Each verified email produces a clean, structured record in your default Apify dataset:

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
  "verification_method": "fast_http",
  "transfer_bytes": 156,
  "proxy_session": "nb_3f1a_1",
  "attempts": 1,
  "latency_seconds": 0.42,
  "error": null
}
```
