---
area: deploy
accepted: by 2026-06-15
---
# ADR #96 — CloudFront security response headers (static export)

**Decision:** Security response headers (`X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`,
and `Strict-Transport-Security` — max-age 1y, includeSubdomains) are applied by
an `AWS::CloudFront::ResponseHeadersPolicy` (`ConsoleUiResponseHeadersPolicy`)
attached to the console distribution's default cache behavior. The `headers()`
block in `next.config.mjs` was removed.

**Why:** the UI is `output: 'export'` (ADR #41) — a pure static export served
from S3 + CloudFront with no Next.js server at runtime. A Next `headers()` block
is silently ignored under static export (Next emits a build warning to that
effect), so the security headers it declared never reached the browser: the
console shipped with no clickjacking (`X-Frame-Options`) or MIME-sniffing
(`nosniff`) protection, while the config gave a false impression they were set.
CloudFront is the only layer that sees every response, so the policy lives there.

**Notes:**
- `X-XSS-Protection` is intentionally dropped — deprecated, ignored by modern
  browsers, and can itself introduce issues (OWASP advises against setting it).
- `Cache-Control` stays on the S3 objects (`sam/deploy-ui.sh`, `ui/deploy.sh`):
  `immutable` for content-hashed `/_next/static`, `must-revalidate` for HTML and
  `config.js`. A single response-headers policy can't express per-path cache
  rules, and the S3 object metadata already does it correctly — so the cache
  half of the old `headers()` block was redundant, not lost.

**Rule:** for a static-export UI, response headers belong at the edge
(CloudFront ResponseHeadersPolicy) or on the S3 object — never in
`next.config.mjs`, where `output: 'export'` will drop them.
