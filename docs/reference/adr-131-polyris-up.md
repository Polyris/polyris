# ADR #131 — `polyris-up` / `polyris-down`: install the platform from prebuilt release assets

> **Status:** ACCEPTED (2026-11-12).
>
> **Amends:** the install path — `scripts/setup-polyris.sh` is removed; `scripts/install.sh` installs only the CLI. ADR #35 step 4: `polyris-deploy` deploys through boto3 change sets instead of `aws cloudformation deploy`.
>
> **Unchanged:** `sam build && sam deploy` from a checkout still works and is the contributor path.

## Context

Before this ADR, installing the platform took five local tools (`git`, `python3`, `node`, `aws`, `sam`), a clone of the repo and an interactive script. That script ran `sam build` (resolving Lambda dependencies locally), `npm ci && npm run build` (building the UI locally), `ui/deploy.sh`, and a separate Cognito user creation. Each step could fail before the first pipeline ran. `polyris-deploy` also shelled out to the `aws` CLI.

Two facts made most of that unnecessary:

1. **`sam build` output is the same for every user.** It can run once in CI per release.
2. **`sam deploy` is an S3 upload plus a CloudFormation change set.** CloudFormation expands the `AWS::Serverless-2016-10-31` transform on the AWS side, so a SAM template deploys through the plain CloudFormation API with `CAPABILITY_AUTO_EXPAND`.

## Decision

### Release assets

On every `v*` tag, `.github/workflows/release.yml` runs `sam build` and the UI build, then `scripts/build_platform_release.py` assembles `polyris-platform-vX.Y.Z.zip` (+ `.sha256`) and attaches it to the tag's GitHub Release:

| Path in the archive | Contents |
|---|---|
| `manifest.json` | `polyris.platform.Manifest`: version, format, and logical id → file maps |
| `template.json` | `sam/template.yaml` converted to JSON, local paths untouched |
| `lambdas/<LogicalId>.zip` | Built code of each `AWS::Serverless::Function` (deterministic zip) |
| `sfn/<LogicalId>.json` | `DefinitionUri` file of each `AWS::Serverless::StateMachine` |
| `ui/` | Static UI export (`ui/out`) |

The contract lives in `polyris/platform.py` (pure, under the coverage floor); both the build script and `polyris-up` import it.

### `polyris-up`

Installs or upgrades the platform from the archive of the **installed** polyris version, so SDK and stack versions always match. It runs from any directory and writes nothing there except its download cache, `~/.cache/polyris/<version>/`.

1. **Target.** The standard boto3 credential chain; `--profile` / `--region` override it. No region anywhere is an error, never a silent `us-east-1`. It prints profile, region and account first.
2. **Stack.** `--stack`, else `<namespace>-<stage>` when `--namespace` / `--stage` are given, else the single stack tagged `polyris:platform=true`. Several tagged stacks and no name is an error.
3. **Inputs.** A new install asks only for Namespace, Stage (default `dev`) and the admin email, validated against bucket naming and the 25-character IAM budget. On update it sends `UsePreviousValue` for every parameter the stack already has. The stack is the only record of the user's choices; there is no local config file. `--param Key=Value` changes one parameter.
4. **Pre-check.** It runs `head_bucket` on both global bucket names before creating anything, so a taken name fails in a second instead of after a rollback.
5. **Artifacts.** They upload to `polyris-artifacts-<account>-<region>` (created if missing, outside the stack: the code must exist before the stack creates the functions; private, SSE-S3). Keys carry the version and a content digest, because CloudFormation updates Lambda code only when the key changes. Every request carries `ExpectedBucketOwner` = this account: the name is predictable and bucket names are global, so another account could otherwise pre-create it, pre-plant code at the content-addressed keys and have it deployed. A bucket owned by someone else stops the install with an error; we chose that over a random-suffix fallback, which would add per-stack state for a case that needs a targeted attacker.
6. **Deploy.** It rewrites `CodeUri` / `DefinitionUri` to the uploaded objects, stamps `Polyris platform v<version>` into the Description, and deploys through a change set. A change set that replaces or removes a DynamoDB table, S3 bucket, the Cognito user pool or the admin user is listed and needs interactive confirmation or `--accept-data-loss` — `--yes` never covers it. An empty change set reports "already up to date".

**Safety rails.**
- An older release never replaces a newer one without `--allow-downgrade` (the version comes from the stack Description).
- Only **platform-shaped** stacks are changed or deleted: tagged, or carrying the platform's own parameters (`Namespace`, `Stage`, `EnableCognitoAuth`). `--stack my-app` on anything else is refused by both commands.
- `--param` can't set `Namespace`, `Stage` (renaming would replace every resource) or the artifact locations.
- The archive is extracted into a temporary directory and renamed into the cache, so an interrupted extraction is never reused.

**Existing and half-finished stacks.**
- A stack created with `sam deploy` has no tag, so it is never discovered. For its Namespace/Stage the bucket pre-check finds the buckets in this account, looks up the stack that uses them, and stops with `polyris-up --stack <name>`. Naming it adopts the stack after confirmation: parameters kept, tag added, `ArtifactsBucket` set, so `UiBootstrap` takes over the UI.
- A tagged stack in `REVIEW_IN_PROGRESS` (install interrupted before its change set ran) is finished as a new install. One in `ROLLBACK_COMPLETE` stops with a pointer to `polyris-down`.

### `polyris-down`

Refuses non-platform and mid-update stacks, and — unless `--yes` — a platform that still has pipelines deployed (their schedules would keep firing against a deleted platform). The pipeline registry is the source of truth (ADR #39): every registered pipeline whose state machine still exists counts, whatever namespace its `config.py` gave it, plus stacks named `<Namespace>-<Stage>-polyris-*`. Lists what it removes (DynamoDB tables are always deleted, since the template has no `DeletionPolicy`; buckets with object counts) and asks for the stack name to be typed back (`--yes` for CI). It empties the UI bucket itself and refuses while `ResultsBucket` holds objects unless `--delete-data` is passed. It deletes the artifacts bucket only when no other platform stack remains in the region.

### Template

- **`UiBootstrap`** (gated by the `ArtifactsBucket` parameter, which `polyris-up` sets) copies the prebuilt UI into `ConsoleUiBucket`, prunes files of the previous build, renders `config.js` from the stack's own values, and invalidates CloudFront. `config.js` therefore follows the stack even when it is updated outside `polyris-up`. Its PhysicalResourceId is stable, and on Delete it empties the bucket only when the parent stack is being torn down. `sam/lambdas/ui_bootstrap/config_js.py` is the canonical `config.js` shape; the heredocs in `ui/deploy.sh` and `sam/deploy-ui.sh` are pinned to it by a parity test.
- **`AdminEmail`** creates the first console user (`AWS::Cognito::UserPoolUser`); Cognito emails the temporary password.
- **No Cognito self sign-up** (`AllowAdminCreateUserOnly`). The console API gives every Cognito user full access, and auth is on by default, so anyone who opened the console URL could otherwise register and administer the platform. Users come from `AdminEmail`, the AWS console or `admin-create-user`. Accounts created while sign-up was open survive the change; operators audit them (CHANGELOG).
- **`AWS::Region`** replaces the `AwsRegion` parameter at every use site, so the region can't drift from the deploy region. `AwsRegion` stays declared, ignored, so existing `samconfig.toml` files keep deploying; it goes in the next major version.

### Pipelines

- **`polyris-deploy` uses boto3 change sets** (`polyris/cfn_changeset.py`, shared with `polyris-up`) instead of the `aws` CLI. Templates over the 51,200-byte inline limit go through the results bucket.
- **Platform discovery.** Without `--stack` or `stack_name` in `config.py`, `polyris-deploy` uses the single tagged platform stack; discovery skips dead stacks (failed create, deleting) and, for deploys, stacks with no resources yet. With no `config.py` at all, a pipeline on a **tagged** (`polyris-up`) platform takes its namespace and stage from that platform's parameters — the discovered one or the one named with `--stack` — so pipelines of two platforms in one account can't share stack names (found in the live smoke). Untagged `sam deploy` platforms keep the pre-131 naming (`polyris-<stage>-polyris-<dag>`, which v1.1.3 produced for `--stack` without `config.py`), and so does a pipeline whose stack already exists under that name — renaming would deploy a second copy next to it; a `--stage` that doesn't match the platform's stage is an error. Without an explicit region it uses the AWS profile's region, then `us-east-1` as before — unlike `polyris-up`, it keeps that last-resort default for compatibility. `config.py` is optional for one platform per region; untagged (`sam deploy`) stacks keep working through `config.py`.
- **`polyris-init <name> --hello`** finds the platform the same way and scaffolds a pipeline on its built-in test Step Function (`TestQuickSfnArn` output), so the first `polyris-deploy` succeeds without editing placeholders.

### Installer

`scripts/install.sh` needs only Python ≥ 3.11. It installs the release's source archive (no git) into its own virtualenv, or with `pipx` when present, and links the commands into `~/.local/bin`. AWS CloudShell works with no local setup once `sudo dnf install -y python3.11` adds a new enough Python.

## Consequences

- An end user needs Python and AWS credentials. SSO users still use the AWS CLI for `aws sso login`.
- Every `v*` tag must carry a platform archive; `polyris-up` for a version without one fails with a clear message. The workflow can be re-run for an existing tag.
- Two paths deploy one template: `polyris-up` (prebuilt, `UiBootstrap`) and from-source `sam deploy` (`ui/deploy.sh`). With `ArtifactsBucket` empty the template behaves as before. A stack moves to `polyris-up` by adoption; moving it back takes a `sam deploy` with `ArtifactsBucket=""` (otherwise `UsePreviousValue` keeps the prebuilt UI path).
- The release repository is fixed in `polyris/platform.py` (`RELEASE_REPO`); a fork installs its own builds with `--archive`.
- CI publishes the release itself, in the order draft → attach the archive, its `.sha256` and `install.sh` → publish, so a published release always has its assets. With GitHub's *Immutable releases* enabled, published assets can't be swapped — this matters because `polyris-up` deploys them with broad IAM rights, and the checksum file lives in the same release. A failed workflow leaves an invisible draft; re-run it for the tag.
- The artifacts bucket keeps every installed release (keys are content-addressed); there is no lifecycle rule yet.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| Keep sam on the user's machine | `sam build` is deterministic, so it belongs in CI; `sam deploy` is S3 + CloudFormation, which boto3 does directly. |
| PyPI as the distribution channel | Deferred, not rejected. Installing a tag's source archive works today; PyPI later changes only the install command. |
| AWS Serverless Application Repository | Doesn't support `AWS::CloudFront::OriginAccessControl`; we would have to downgrade to the legacy OAI. |
| AWS Marketplace | No listing type fits a self-hosted serverless CloudFormation product: CloudFormation delivery is tied to AMIs, and the SaaS route requires a seller-run control plane and registration service. |
| A local AWS emulator to try polyris | LocalStack requires an auth token since 2026.03; AWS lists Step Functions Local as unsupported. `polyris-local` covers offline validation and mock runs. |
| Save inputs to a local file | Ties upgrades to one machine and folder; the stack's parameters already hold the values. |
| Upload the UI from `polyris-up` | `config.js` would drift whenever the stack is updated by other means. |
| Artifacts bucket inside the platform stack | The Lambda code must exist before the stack creates the functions. |
| Per-version stack tag | Stack tags propagate to every taggable resource; a changing value would touch them all on each upgrade. The version lives in the stack Description. |

## Out of scope

- A Launch Stack button (needs a public artifacts bucket in every region, because Lambda code must be in the function's region).
- `DeletionPolicy` / `UpdateReplacePolicy` on the tables and `ResultsBucket`.
- Real values in `polyris-init --project` (it still writes placeholders; `--hello` and platform discovery cover the first-run path).
