# Security Policy

## Supported Versions

Polyris is pre-1.0 software. Security fixes are applied to the latest released
version only. We recommend always running the most recent release.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest | :x:               |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately through one of the following channels:

<!-- TODO(release): replace with your real security contact before publishing -->
- **GitHub Security Advisories** (preferred): use the
  ["Report a vulnerability"](https://github.com/Polyris/polyris/security/advisories/new)
  button on the repository's *Security* tab.
- **Email**: makskoval@quintagroup.org

Please include as much of the following as you can:

- The type of issue (e.g. injection, privilege escalation, secret exposure, SSRF).
- The affected component (SDK, Console API, generated CloudFormation/Step Functions, UI).
- Step-by-step instructions to reproduce, and a proof-of-concept if possible.
- The impact, including how an attacker might exploit it.

## What to Expect

- **Acknowledgement** within 3 business days.
- An initial assessment and severity classification within 10 business days.
- Coordinated disclosure: we will work with you on a fix and a public advisory,
  and credit you in the release notes unless you prefer to remain anonymous.

Please give us a reasonable window to remediate before any public disclosure.

## Scope

In scope:

- The Polyris SDK (`polyris/`) and its generated Step Functions / CloudFormation output.
- The Console API Lambda (`sam/lambdas/console_api/`) and supporting Lambdas.
- The Console UI (`ui/`).
- Deployment tooling and example infrastructure in this repository.

Out of scope:

- Vulnerabilities in third-party dependencies (report those upstream; we will
  update once a fix is available).
- Issues that require physical access to a user's machine or a compromised AWS
  account.
- Misconfigurations in a user's own AWS environment that are not caused by
  Polyris's defaults.

## `npm audit` findings — dev/build toolchain only

The Console UI ships as a **static export** (`output: 'export'` in
`ui/next.config.js`): the deployed site is plain HTML, CSS, and JavaScript served from
S3/CloudFront. There is **no Next.js server at runtime** — no middleware, no proxy, no
image-optimisation pipeline, and no build or test toolchain in production.

As of v0.93.1 `npm audit` reports **3 high, 0 critical**, all in the build toolchain:

| Package | Advisory | Why it does not reach a deployed instance |
|---|---|---|
| `next` | Middleware / proxy bypass in App Router | Requires the Next.js server. A static export has none; there is no middleware to bypass. No patched 16.2.x exists — 16.2.11 is current, and npm's suggested "fix" is a downgrade to 9.3.3, which we reject. |
| `postcss` | XSS via unescaped `</style>` in stringify output | Build-time only. The CSS input is our own source, not user input; the output is a static asset. |
| `sharp` | Inherited libvips CVEs | Pulled in by `next` for image optimisation, which `output: 'export'` disables. Never invoked. |

The previously-reported `vitest` / `vite` / `esbuild` criticals were cleared in v0.93.1 by
migrating the test runner to `vitest` 4 and dropping `@vitejs/plugin-react` from the test
config (Vitest transforms JSX natively; the Babel plugin was a dev-server concern and
emitted deprecation warnings under Vite 7).

We treat the remaining findings as **low-priority, non-runtime** and re-evaluate them on
each dependency bump. A vulnerability in a **runtime** dependency — anything that reaches
the shipped bundle or the backend Lambdas — is treated with priority and should be
reported as above.
