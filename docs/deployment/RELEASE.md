# Release process

How a polyris version is cut and what each release publishes. Read this before tagging.

## Cut a release

```bash
./scripts/release.sh full v1.2.0     # all gates (make check), or `lib` for SDK-only gates
```

`release.sh` runs the gates, bumps the version in `pyproject.toml`, `polyris/__init__.py`, `ui/package.json` and the `polyris @ …@vX.Y.Z` pin in `sam/lambdas/console_api/requirements.txt`, commits, tags, pushes, creates the GitHub Release (or reuses it) and attaches `install.sh`.

Before tagging, smoke-test current `main` on a dev account (root `CLAUDE.md` Principle #15).

## What the tag publishes

Pushing the `v*` tag starts [`.github/workflows/release.yml`](../../.github/workflows/release.yml). It builds the platform release that `polyris-up` installs and attaches it to the tag's GitHub Release:

| Asset | Contents |
|---|---|
| `polyris-platform-vX.Y.Z.zip` | `manifest.json`, the template as JSON, one zip per Lambda (built by `sam build`), the Step Functions definitions, the static UI (`ui/out`) |
| `polyris-platform-vX.Y.Z.zip.sha256` | Checksum `polyris-up` verifies after download |
| `install.sh` | The CLI installer (attached by `release.sh`) |

`polyris-up` downloads the archive whose version equals the installed polyris version, so **every `v*` tag must carry one**. If the workflow fails, fix the cause and re-run it for the existing tag: Actions → *Release artifacts* → *Run workflow* → tag `v1.2.0`.

The workflow refuses to build when the tag doesn't match the version in `pyproject.toml`.

Archive layout and the `polyris-up` side of the contract: [ADR #131](../reference/adr-131-polyris-up.md), `polyris/platform.py` and `scripts/build_platform_release.py`.

## Build the archive locally

To test an unreleased build end to end:

```bash
(cd sam && sam build --build-dir ../build/sam)
(cd ui && npm ci && npm run build)
python scripts/build_platform_release.py --version 0.0.0-dev --sam-build-dir build/sam --ui-dir ui/out --out dist
polyris-up --archive dist/polyris-platform-v0.0.0-dev.zip --namespace mydev
```
