# Release Process

This covers two distinct things: **publishing the open-source repo** (the free
source tree) and **publishing deployable artifacts** (CloudFormation template + UI
for users to launch). They are separate.

## Artifact release (tag → S3 + Launch Stack)

Polyris uses GitHub Actions to publish releases automatically on `git tag`.

## One-time setup

### 1. Deploy bootstrap stack

```bash
cd sam
aws cloudformation deploy \
  --template-file bootstrap.yaml \
  --stack-name polyris-bootstrap \
  --parameter-overrides \
    GitHubOrg=Polyris \
    GitHubRepo=polyris \
    ReleasesBucketName=polyris-releases \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

This creates:
- Public S3 bucket `polyris-releases` for release artifacts
- GitHub OIDC provider (one per AWS account)
- IAM role `polyris-github-release` for GitHub Actions

### 2. Get outputs

```bash
aws cloudformation describe-stacks \
  --stack-name polyris-bootstrap \
  --query "Stacks[0].Outputs" \
  --output table
```

### 3. Add GitHub Secrets

In your GitHub repo → Settings → Secrets → Actions → New repository secret:

| Secret | Value from outputs |
|--------|-------------------|
| `AWS_RELEASE_ROLE_ARN` | `GitHubReleaseRoleArn` output |
| `RELEASES_BUCKET` | `ReleasesBucketName` output |

---

## Publishing a release

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions automatically:
1. Builds UI (`npm run build`) — ensures consistent build environment
2. Packages SAM template (`sam package`) — uploads Lambda ZIPs and SFN templates to S3, replaces local paths with S3 URLs
3. Publishes `template.yaml` to `s3://polyris-releases/v1.0.0/template.yaml`
4. Publishes UI to `s3://polyris-releases/v1.0.0/ui/`
5. Updates `latest/` pointer
6. Creates GitHub Release with Launch Stack button and `ui-dist.zip` for Tier 2

## Launch Stack URL

After publishing `v1.0.0`:
```
https://console.aws.amazon.com/cloudformation/home#/stacks/create?templateURL=https://polyris-releases.s3.amazonaws.com/v1.0.0/template.yaml
```

This URL is automatically included in the GitHub Release notes.

## Pre-releases

Tags with `-` are automatically marked as pre-release:
```bash
git tag v1.0.0-beta.1
git push origin v1.0.0-beta.1
```

## Notes

- **OIDC** — no long-lived AWS credentials stored in GitHub. GitHub gets temporary credentials per run.
- **Scope** — role only allows pushes from your repo's tags and main branch.
- **OIDC provider** — one per AWS account. If it already exists, comment out `GitHubOidcProvider` in `bootstrap.yaml`.
