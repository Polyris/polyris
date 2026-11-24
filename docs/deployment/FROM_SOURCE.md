# Deploy polyris from source

How to run polyris from a checkout of this repo — to test a change to the template, a Lambda or the UI before it is released. To install a released version, use [QUICKSTART.md](../getting-started/QUICKSTART.md) instead.

You need the AWS SAM CLI, Node.js 20.19+, Python 3.11+ and AWS credentials.

## Deploy with SAM

```bash
git clone https://github.com/Polyris/polyris.git && cd polyris
pip install -e .

# The console and coordination Lambdas install polyris from their requirements.txt,
# pinned to the last release: point both at this checkout. Don't commit this change;
# undo it with `git checkout -- sam/lambdas/*/requirements.txt` before you commit.
sed -i "s|^polyris @ .*|polyris @ file://$PWD|" \
  sam/lambdas/console_api/requirements.txt sam/lambdas/coordination/requirements.txt

cd sam
cp samconfig.toml.example samconfig.toml   # set stack_name, Namespace, Stage, profile
sam build && sam deploy

cd ../ui
npm ci && npm run build
./deploy.sh <stack-name> <region> ./out --profile <profile>
```

`ui/deploy.sh` reads the stack outputs and writes `config.js`; it reads no `config.py`, so pass it the stack and the region. Run it again after each `sam deploy` that changes an output.

Namespace + Stage may be at most 33 characters (IAM role names are capped at 64).

## Pipelines on a stack deployed with SAM

A stack deployed with `sam deploy` isn't tagged as a `polyris-up` platform, so `polyris-deploy` does not find it on its own. Name it in `config.py` ([CONFIGURATION.md](../reference/CONFIGURATION.md)), or pass `--stack`:

```python
ENVIRONMENTS = {
    "dev": {
        "stack_name": "mycompany-dev",  # the stack_name of sam/samconfig.toml
        "namespace": "mycompany",
        "region": "us-east-1",           # the region of that stack
    },
}
```

`stack_name` must equal the `stack_name` in `sam/samconfig.toml`. polyris cannot check this when the pipelines and the checkout live in different repositories. To manage such a stack with `polyris-up` from then on: [manage-the-platform.md](../how-to/manage-the-platform.md).

## Test the release path

To check what `polyris-up` will install, build the archive CI publishes on a tag and install it:

```bash
(cd sam && sam build --build-dir ../build/sam)
(cd ui && npm ci && npm run build)
python scripts/build_platform_release.py --version 0.0.0-dev \
  --sam-build-dir build/sam --ui-dir ui/out --out dist
polyris-up --archive dist/polyris-platform-v0.0.0-dev.zip --namespace mydev
```

Remove it with `polyris-down --stack mydev-dev --delete-data`. How releases are built: [RELEASE.md](RELEASE.md).
