# sindarin-demo-app

The sample application the Sindarin demo builds a feature in (DDD §16.3, §18.1). A tiny
orders service with one seeded feature to build: **CSV export of orders**. The endpoint is
stubbed and its tests fail, so the `coder` agent's work is real and small.

Acceptance criteria (`SPEC-1`):

1. `GET /orders.csv` returns `text/csv`
2. a header row is present even when there are no orders
3. exports above 10,000 rows return `413` with a message
4. a test covers each of the above

## Put it on GitHub

`demo-app/` is a folder inside the Sindarin tree (DDD §16.3), not a repository of its own.
Push it from a copy so the folder here stays plain files rather than an embedded repository:

```
rm -rf /tmp/sindarin-demo-app && cp -R demo-app /tmp/sindarin-demo-app && rm -rf /tmp/sindarin-demo-app/.pytest_cache
cd /tmp/sindarin-demo-app
git init -b main && git add -A && git commit -m "sindarin-demo-app: orders service with CSV export stub"
gh repo create <you>/sindarin-demo-app --public --source=. --push
```

The coder's working clone (`CODER_WORKDIR` in `agents/coder/.env`) is the copy that changes
afterwards; this folder is the reference source.

Then protect `main` so only `sindarin-github`'s token can merge (DDD §4.1 layer 2):
Settings → Branches → add a branch protection rule for `main` → require a pull request before
merging, and restrict who can push to the identity whose token sits in `executor/github.env`.
The coder's token (`agents/coder/.env`, `CODER_GITHUB_TOKEN`) gets contents and pull requests
write and nothing else; with branch protection in place it cannot touch `main`.

Set the repository name in `agents/policy/policy.yaml` (`allowed_repos`, `repo`) and run
`uv run sindarin adapter smoke` from the Sindarin repo to check visibility, push rights and the
branch protection, read-only.

## Run the tests

```
uv run pytest -q     # 4 tests, all failing until the feature is built
```
