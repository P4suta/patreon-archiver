# Development

The runtime image pins Python and uv in `Dockerfile`.
CI reads the Python version from `.python-version`.
Workflow steps pin uv alongside each action.
The supported Python range starts at 3.13.
`uv.lock` records the exact dependency graph.

## Python checks

Install the pinned tools, then run:

```sh
uv sync --group dev --locked
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked pyright
uv run --locked pytest
```

Pyright checks the scripts in strict mode.
Pytest requires 100% line and branch coverage and uses a fixed Hypothesis seed.
Tests use synthetic data and temporary directories.
Local Git hooks use these same locked Python commands.

Build the image before running the offline container integration tests:

```sh
docker compose build pa
docker compose run --rm pa smoke
uv run --locked pytest
```

Container integration tests skip when Docker or the local image is unavailable.
They use isolated temporary data directories.
A unit-test run with those skips doesn't establish container integration coverage.

The optional devcontainer uses the Dockerfile's `dev` target.
Rebuild it after changing its base image or bundled tools.

## Other checks

`mise.toml` pins the fast-check tool versions.

```sh
mise x -- actionlint
mise x -- shellcheck -x scripts/*.sh
mise x -- hadolint --config .hadolint.yaml --failure-threshold error Dockerfile
uvx --from yamllint==1.38.0 yamllint --config-file .yamllint.yaml --strict .
mise x -- typos
mise x -- gitleaks detect --source . --redact --no-git
```

CI pins these tools and selects Python, runtime-image, and security checks from affected paths.
Runtime-image jobs also run the eleven container integration cases without network access.
The separate Python job enforces full line and branch coverage.
Fast workflow, shell, container-file, spelling, and secret checks run on every pull request.
The `required` job rejects selected failures and unexpected skips.
Manual CI can run all checks or security analysis only.
See the [verification decision](adr/verification-contracts.md).

## Dependency maintenance

Renovate inherits the shared dependency preset.
Python runtime updates stay within 3.13, while package metadata retains its compatibility floor.
Keep Python version pins aligned in the Docker runtime, `.python-version`, and CI.
Update uv in both CI and the Dockerfile.

Review yt-dlp updates explicitly.
Update its project pin and lockfile together, run all checks, rebuild the runtime image, and run the offline smoke and integration tests.
Offline tests don't prove compatibility with a live publisher or a user's credentials.

Pull requests build a local test image without registry credentials.
Container publication remains a separate workflow for version tags and manual requests.
