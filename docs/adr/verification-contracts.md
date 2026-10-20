---
date: 2026-10-21
---

# Select verification by affected contracts

## Status

Proposed.

## Context

The application is distributed as a Linux x86-64 container with a Python 3.13 runtime.
Documentation edits previously started the full test suite, while main pushes repeated checks and published an image.
Managed security analysis added another main run and a weekly scan.

The required status must distinguish an intentionally unselected job from missing verification.
Pull request verification must have no registry publication credentials.

## Decision

Run fast static checks on every pull request and merge-group event.
Select Python, runtime-image, and CodeQL checks using the [shared path selector](../../.github/filters/ci.yml).
Changes to the selector or CI workflow select every contract.
Keep the existing strict typing, tests, and full branch coverage.

Build selected runtime images without publication, then run smoke and existing container integration checks with networking disabled.
Bundled source and container fixture changes select this contract.
Analyze Python and Actions with the same default CodeQL query suite as the managed setup.
Replace managed scanning after the advanced workflow is available.
Provide manual all-checks and security-only modes.
Remove scheduled and duplicate main verification.

Require success for each selected job and an explicit skipped result for each unselected job.
Reject invalid selector outputs and invalid manual modes in the required gate.
The [workflow](../../.github/workflows/lint.yml) is the authoritative decision and gate implementation.

Keep Python's declared compatibility floor while pinning matching runtime, local, and CI SDKs.
Keep registry publication in the separate version-tag and manual workflow.

## Consequences

Documentation changes retain fast checks without rebuilding the image.
Dependency and runtime changes still exercise the affected contracts.
Security-only dispatches can establish a default-branch baseline without rebuilding or publishing.

Running every job on every event was rejected because it repeats work without changing the affected contract.
Skipping the whole required workflow was rejected because it cannot distinguish an intentional omission from a missing result.
Path selection must be maintained when new runtime or security inputs are added.
Offline verification does not establish live extractor compatibility or release authorization.
