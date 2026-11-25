# ADR #41 — URL Routing: CloudFront Function + window.history for Deep State

**Decision (v0.71.8):** Use a CloudFront Function for SPA URL rewriting at the edge, Next.js file-system routes for top-level navigation, and `window.history.pushState/replaceState` directly for deep state in URL search params.

**Context:** Until v0.70.x all top-level navigation lived in Zustand (`mainView` field). URLs looked like `/?view=assets&pipeline=etl_main` on root path. Two issues:
1. URLs read like an admin tool, not a product
2. Refresh-stability of deep state was fragile

We wanted product-grade URLs (`/pipelines/etl_main`) without infrastructure surprises.

**What didn't work (v0.71.0 through v0.71.7):**

Seven attempts tried frontend-only solutions:
- v0.71.0–3: store ↔ URL bidirectional sync hooks
- v0.71.4: path → query param hotfix
- v0.71.5: trailing-slash pathname normalization
- v0.71.6: trailing-slash target URLs + RootPage `[]` deps fix
- v0.71.7: full clean rebuild without sync hooks

Each version exposed a new bug in production. v0.71.7 fixed the loop but URL still didn't update on pipeline click and executions list disappeared. The pattern revealed by debugging: any client-side URL strategy fails because the server (S3 via CloudFront) cannot find files for path-based URLs.

**Root cause:**

The deployment is `next build` with `output: 'export'` → static files in S3, served via CloudFront with **OAC and S3 REST origin** (not the S3 website endpoint).

- S3 REST endpoint does NOT auto-resolve `index.html` inside folders.
  `GET /pipelines/` returns 404 because there's no S3 key matching that exact path; `index.html` resolution is a *website endpoint* feature.
- CloudFront's `CustomErrorResponses` (404 → /index.html) then serves **root** `/index.html` regardless of requested path.
- Next.js with `output: 'export'` only generates HTML for routes with corresponding `page.tsx`. `/pipelines/index.html` exists if `app/pipelines/page.tsx` exists, but `/pipelines/etl_main/index.html` does not.
- `router.push('/pipelines/etl_main')` triggers an RSC prefetch (`?_rsc=...`) for the manifest. The manifest doesn't include this dynamic path → fallback path → loop.

No frontend-only fix can resolve this. The server has to know how to serve `/pipelines/anything` → `/pipelines/index.html`.

**Options considered:**

1. **Switch S3 origin to website endpoint:** Has automatic `index.html` resolution but loses OAC (must use bucket policy with public read or signed URLs). Significant security regression.

2. **Lambda@Edge with URL rewrite:** Works but cold-start latency, more expensive, more complex IAM, more failure modes.

3. **Dynamic Next routes (`app/pipelines/[name]/page.tsx`):** Requires `generateStaticParams` to enumerate all pipeline names at build time. Pipeline names are runtime data, not build-time. Returning `[]` doesn't work with `output: 'export'`.

4. **CloudFront Function with URL rewrite (chosen):**
   - Runs at edge, ~1ms latency overhead
   - ~$0.10 per million requests (effectively free at our scale)
   - Inline JavaScript, no IAM role, no Lambda, no CloudWatch logs needed
   - Validated and published as part of CloudFormation deploy
   - Disassociation is one click in Console for instant rollback

**Implementation:**

```yaml
ConsoleUiUrlRewriteFunction:
  Type: AWS::CloudFront::Function
  Properties:
    AutoPublish: true
    FunctionConfig:
      Runtime: cloudfront-js-2.0
    FunctionCode: |
      function handler(event) {
        var req = event.request;
        var uri = req.uri;
        var match = uri.match(/^\/(pipelines|assets|tasks|runs)(\/[^.]*)?$/);
        if (match) {
          req.uri = '/' + match[1] + '/index.html';
        }
        return req;
      }
```

The regex matches `/pipelines`, `/pipelines/`, `/pipelines/foo`, `/pipelines/foo/bar` (any depth, any name) but NOT paths with file extensions (`.html`, `.js`, `.css` etc). `/_next/static/foo.js` passes through because it doesn't start with one of the four view prefixes anyway.

Associated as `viewer-request` event handler on the default cache behavior.

**Frontend pattern (v0.71.8):**

- Top-level routes: `app/pipelines/page.tsx`, `app/assets/page.tsx`,
  `app/tasks/page.tsx`, `app/runs/page.tsx`. Each renders `<App />`.
- `App.tsx` uses `usePathname()` to determine which view to render.
- `mainView` removed from Zustand store. Pathname is the source of truth.
- Top-level navigation: `router.push('/{view}/')`.
- Deep state in URL: `useUrlSync` writes via `window.history.pushState/replaceState` directly (no Next router involvement → no RSC prefetch → no loop).

**Why deep state via `window.history`:**

Even with the CloudFront Function in place, using `router.push` for search-param-only changes (e.g., `?pipeline=etl_main`) triggers Next's RSC prefetch logic. Static export doesn't generate the prefetch payloads → 404. We could disable prefetch but `window.history` is simpler — it just updates the URL bar with no network involvement, and our app reads search params on mount.

**URL examples:**

| URL | Meaning |
|---|---|
| `/pipelines/` | Pipelines view |
| `/pipelines/?pipeline=etl_main` | Pipelines view, etl_main selected |
| `/pipelines/?pipeline=etl_main&mode=gantt&date=2025-04-15&execution=exec-abc` | Specific run view |
| `/assets/` | Assets catalog |
| `/tasks/` | All tasks |
| `/runs/` | All runs |

**Trade-offs:**

- ✅ Path-based top-level URLs (product-grade UX)
- ✅ Refresh stable on every URL
- ✅ Browser back/forward works
- ✅ Cmd+click on tab opens new browser tab
- ✅ Stable on production CloudFront + S3 OAC + static export
- ⚠️ Adds infrastructure dependency (CloudFront Function)
- ⚠️ Asset detail (selectedPartition, selectedEvent) and filters
  (taskFilter, runFilter) are not in URL — deferred to future
- ⚠️ Deploy must be ordered: SAM first (Function active), then frontend.
  Reverse order = 404 loop until Function activates.

**Rollback paths:**

1. **Console disassociation** (fastest, ~5 min for edge propagation):
   AWS Console → CloudFront → distribution → Behaviors → Edit → remove
   Function association → Save. Then redeploy v0.70.18 frontend.

2. **CFN redeploy** (~10 min): Extract `polyris_baseline_v70_18.tar.gz`,
   `sam deploy && deploy.sh && invalidation`.

**Lesson for future routing changes:**

With static export + S3 + CloudFront REST origin, only routes that
have build-time HTML files can serve as path segments. Anything dynamic
(specific pipeline name, asset name) must go in URL search params,
updated via `window.history` (not Next router) to avoid RSC prefetch
404 loops.

If we ever want `/pipelines/etl_main` (asset name in path), the options
are: (a) extend the CloudFront Function to also serve `/pipelines/etl_main`
→ `/pipelines/index.html`, or (b) add a build step that pre-generates
HTML for known pipeline names. (a) is simpler if the in-app router can
read the pathname segment as the pipeline ID.
