# Release process

How a polyris version is cut and what each release publishes. Read this before tagging.

## Cut a release

```bash
./scripts/release.sh full v1.2.0     # all gates (make check), or `lib` for SDK-only gates
```

`release.sh` runs the gates, bumps the version in `pyproject.toml`, `polyris/__init__.py`, `ui/package.json` and the `polyris @ …@vX.Y.Z` pin in `sam/lambdas/console_api/requirements.txt`, commits, tags and pushes. It doesn't create the GitHub Release: CI does that from the tag (below).

Before tagging, smoke-test current `main` on a dev account (root `CLAUDE.md` Principle #15).

## What the tag publishes

Pushing the `v*` tag starts [`.github/workflows/release.yml`](../../.github/workflows/release.yml). It builds the platform archive, creates the GitHub Release as a **draft** (notes generated from PR titles and commits), attaches every asset, and only then **publishes** it — 5–10 minutes after the push. Watch it with `gh run list --workflow release.yml`.

| Asset | Contents |
|---|---|
| `polyris-platform-vX.Y.Z.zip` | `manifest.json`, the template as JSON, one zip per Lambda (built by `sam build`), the Step Functions definitions, the static UI (`ui/out`) |
| `polyris-platform-vX.Y.Z.zip.sha256` | Checksum `polyris-up` verifies after download |
| `install.sh` | The CLI installer from this tag |

`polyris-up` downloads the archive whose version equals the installed polyris version, so **every `v*` tag must carry one**. Because the release stays a draft until its assets are attached, a published release is never missing them. If the workflow fails, the release stays a draft (invisible to users): fix the cause and re-run it for the existing tag — Actions → *Release artifacts* → *Run workflow* → tag `v1.2.0`.

**Repository settings.** Keep **Settings → General → Releases → Immutable releases** enabled. Once published, a release's files and tag can't be changed. Nobody, not even someone with write access, can then swap the archive `polyris-up` deploys into users' AWS accounts; its `.sha256` sits in the same release and would be swapped with it. The draft-then-publish order is what makes this work. The workflow also needs to write releases; it requests `contents: write` itself.

The workflow refuses to build when the tag doesn't match the version in `pyproject.toml`.

Every pull request runs the same build as a trial (CI job *Platform archive (trial build)*): `sam build`, the archive script, and `cfn-lint` on the template exactly as `polyris-up` would deploy it. Nothing is published; a change that would break the release fails in the PR instead of on tag day.

Archive layout and the `polyris-up` side of the contract: [ADR #131](../reference/adr-131-polyris-up.md), `polyris/platform.py` and `scripts/build_platform_release.py`.

## Build the archive locally

To test an unreleased build end to end:

```bash
(cd sam && sam build --build-dir ../build/sam)
(cd ui && npm ci && npm run build)
python scripts/build_platform_release.py --version 0.0.0-dev --sam-build-dir build/sam --ui-dir ui/out --out dist
polyris-up --archive dist/polyris-platform-v0.0.0-dev.zip --namespace mydev
```
