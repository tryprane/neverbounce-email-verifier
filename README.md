# NeverBounce Email Verifier Actor

A lightning-fast, zero-error email deliverability verification Actor powered by **NeverBounce** and **Scrapling StealthyFetcher**, with automated **Apify Residential Proxy rotation**.

## 🚀 Key Features
- **Accurate Real-Time Verification**: Retrieves live deliverability status (`valid`, `invalid`, `disposable`, `catch_all`, `unknown`) directly from NeverBounce.
- **Detailed MX & SMTP Flags**: Provides granular verification flags:
  - `smtp_connectable`: Confirms the destination mail server accepts incoming mail.
  - `has_dns` & `has_dns_mx`: Verifies DNS and MX records.
  - `free_email`: Identifies personal webmail (Gmail, Yahoo, Hotmail).
  - `role_account`: Detects generic addresses (admin@, support@, info@).
- **Stealth Architecture**: Powered by Scrapling to bypass PerimeterX (HUMAN Security) and Cloudflare challenges without detection.
- **Zero Errors & Automatic Proxy Rotation**: Automatically rotates Apify Residential Proxy sessions on any rate-limiting (`429`) or challenge (`403`), guaranteeing continuous runs without failure.
- **Batch Processing**: Accepts single emails or bulk email lists.

---

## 📥 Input Parameters

| Field | Type | Description |
| :--- | :--- | :--- |
| `email` | String | Single email address (e.g. `support@neverbounce.com`). |
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
    "invalid.test.fake.999@gmail.com"
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
  "flags": [
    "has_dns",
    "has_dns_mx",
    "smtp_connectable",
    "historical_response"
  ],
  "free_email": false,
  "role_account": false,
  "smtp_connectable": true,
  "has_dns": true,
  "has_dns_mx": true,
  "proxy_session": "nb_a1b2_1",
  "attempts": 1,
  "latency_seconds": 2.15,
  "error": null
}
```
