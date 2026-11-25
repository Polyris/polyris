# ADR #65 — Top-level view registration — three-place sync (v0.78.6)

#### Context

Top-level views in the console (Pipelines, Assets, Tasks, Runs, Backfills)
are file-system routes under Next.js static export (`output: 'export'`,
`trailingSlash: true`). Each route generates `/{view}/index.html` and is
served from S3 behind CloudFront.

The view list lives in **three** places that must stay in sync:

1. **`ui/src/types/index.ts` — `MAIN_VIEWS`** — runtime guard used by
   `isMainView()` and `viewFromPathname()`. Drives Header tab rendering
   and App.tsx render switch.
2. **`sam/template.yaml` — `ConsoleUiUrlRewriteFunction.FunctionCode`** —
   regex `^\/(pipelines|...)(\/...)?$` that rewrites SPA deep paths
   (`/pipelines/anything`) to the corresponding `/{view}/index.html` so
   S3 finds the file. Without this rewrite, `/backfills/` (directory
   path) returns 404 → CloudFront `CustomErrorResponses` converts to
   200 + `/index.html` → RootPage component → redirect to `/pipelines/`.
3. **`ui/src/app/page.tsx` — `validViews`** — legacy-bookmark fallback
   for `/?view=backfills` URLs. If a view is missing here, the legacy
   URL ignores it and redirects to default `/pipelines/`.

#### Decision

When adding a top-level view to App.tsx mainView, all three locations
**must** be updated atomically in the same PR. Order doesn't matter
mechanically but **deploy must include both backend (template.yaml) and
frontend (page.tsx + types) in the same release**.

#### Why this matters

Backfills view was added in v0.75.x to App.tsx mainView, Header tabs,
and BackfillsPage Next route — but the CloudFront function and root
page were never updated. The result: clicking the Backfills tab took
the user to `/backfills/`, where S3 returned 404, CloudFront
masqueraded as 200, and the RootPage component instantly redirected
back to `/pipelines/`. User-visible behavior: "click Backfills tab,
end up on Pipelines tab". Subtle enough that it went unnoticed for
months because the URL flicker is brief.

Fixed in v0.78.6 by adding `backfills` to both:
- `sam/template.yaml`: regex alternation in `ConsoleUiUrlRewriteFunction`
- `ui/src/app/page.tsx`: `validViews` array

#### Prevention

`CLAUDE.md` adds a rule about the three-place sync. The CloudFront
function file includes an inline `⚠️` comment naming the other two
files. A future test could enforce by parsing all three sources and
comparing the lists, but YAGNI — explicit comments + ADR + CLAUDE.md
rule should be enough.

#### Hard-learned lesson

Path-based SPA routing with static export has a sharp edge: every new
route requires updates in places that don't share types or imports.
Type-safety helps within the TS bundle but stops at the
infrastructure-as-code boundary. The CloudFront function is plain JS
in a YAML string — neither TypeScript nor any linter catches the drift.
The only mitigation is explicit cross-reference comments + a CLAUDE.md
rule that's checked at PR review time.
