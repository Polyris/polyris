# Polyris Console

The web console of polyris: React 19 and Next.js 16 as a static export, React Query 5 for server state, Zustand 5 for UI state, ReactFlow for graphs, Tailwind and shadcn/ui.

- Using the console: [docs/operations/UI.md](../docs/operations/UI.md)
- Working on it — stack, state, components, tests, and running it against a deployed API: [docs/tools/DEVELOPMENT.md](../docs/tools/DEVELOPMENT.md#console-ui)
- Rules every change follows: [CLAUDE.md](CLAUDE.md)

```bash
npm ci
npm run dev           # http://localhost:3000, with the API in .env.local
npm run typecheck
npm run test:run
npm run lint
npm run build         # the static export, in out/
```
