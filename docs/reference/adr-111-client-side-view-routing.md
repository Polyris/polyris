# ADR #111 — Client-side view routing via the History API

> **Status:** ACCEPTED — implemented. Navigation between top-level views is now
> driven by `history.pushState` (a small `RouteProvider` / `useClientRoute`)
> instead of the Next.js router. No backend, IAM, DynamoDB, or SFN changes. The
> CloudFront SPA rewrite (ADR #41) is unchanged and is exactly what this relies on.

## Context

The console is a Next.js `output: 'export'` static site served from S3 behind a
CloudFront SPA rewrite (ADR #41): any `/{view}/…` path is served the pre-rendered
`/{view}/index.html`. Every view already renders entirely client-side behind the
`AuthGate`, and each route's `page.tsx` renders the same `<App/>`, which derives the
active view from the pathname.

In this setup the Next.js app-router performed a **full document navigation** on every
switch between top-level views (`/pipelines` → `/tasks` → …): a network trace showed the
target `index.html` re-requested, all JS re-executed, the Amplify session re-checked, and
data re-fetched — ~1.5s per navigation, plus the auth-loading splash re-appearing because
the whole provider tree re-mounted. The likely cause is the interaction between the static
export's soft-navigation data requests and the CloudFront rewrite; regardless, the result
was that view switches were full reloads.

Deep state *within* `/pipelines` (`?pipeline=&date=&mode=`) was already client-side via
`useUrlSync` (pushState + popstate). Cross-view navigations that carry deep state must set the
store **directly** before navigating (as `navigateToExecution` already did), rather than
pushing a `/pipelines/?pipeline=…&date=…` URL and relying on a full-reload URL→store read —
which no longer happens under client routing. The Team backfill drill (ADR #63/#68) did the
latter, so it is re-wired here to call `onNavigateToExecution` (a new required
`BackfillsView` prop, `ee-contract.ts`) which sets the store directly.

## Decision

Drive navigation between views ourselves with the History API:

- `RouteProvider` owns the current `pathname` in React state; `push`/`replace` call
  `history.pushState`/`replaceState` and update that state; a `popstate` listener reflects
  browser back/forward. `useClientRoute()` exposes `{ pathname, push, replace }`.
- `usePathname()` and the view-level `router.push`/`router.replace` calls in `App`,
  `AppNav`, `Header`, `AllRunsView`, and `useStoreInit` now use `useClientRoute`.
- Because Next.js never sees a route change, the mounted `page.tsx` (and `<App/>`) stays
  mounted and merely re-renders with the new pathname — so view switches are instant, with
  no reload, re-mount, or re-fetch.
- `useUrlSync` is unchanged (already client-side). The root `/` redirect page keeps the
  Next.js router — it fires only on a bare `/` entry, not on view navigation.

## Consequences

- **Instant view navigation.** No document reload, no provider re-mount, no auth re-check,
  React Query cache preserved.
- **Direct loads and deep links unchanged.** Each route still exports its own `index.html`;
  CloudFront serves it; `App` reads `window.location` on mount. Back/forward works via
  `popstate`.
- **We own routing between views.** New top-level views must be wired through
  `useClientRoute` (and still need a `page.tsx` for the static export + the ADR #41 regex).
- The Next.js router remains only for the initial `/` redirect.
