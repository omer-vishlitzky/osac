# .github/ Agent Context

This area is part of the OSAC monorepo, not an isolated project. Apply the
repository-wide rules in [`../AGENTS.md`](../AGENTS.md), consider downstream
effects, and follow the instructions for every affected component.

## E2E execution

Full-install E2E runs directly for non-draft pull requests on `opened`,
`ready_for_review`, `synchronize`, and `reopened`. Pull-request runs use
GitHub's synthetic PR merge ref without changing the contributor's branch.
Draft PRs skip E2E. The same workflows run on `merge_group`, using GitHub's
fresh temporary merge-queue ref against the latest `main`.

`/ok-to-test` remains the fork secret authorization command. Path filtering
skips docs and unit-test-only changes, while `tests/e2e/**` must still set
`should-run` through the `e2e-suite` filter.

## Release safety

Nightly builds use provisional `sha-*` image tags while all build, unit,
integration, security, and E2E gates run. Promote images to release-looking
nightly tags only after every required gate passes; failed runs must not publish
release-looking tags.

A real, permanent `<component>/vX.Y.Z` tag (release mode's `component_versions`
bump) must be pushed with real actor credentials, not the default
`GITHUB_TOKEN` -- GitHub does not fire push-triggered workflows for a ref
created by `GITHUB_TOKEN`, so a component's own image/binary/proto publish
workflow would silently never run even though the tag exists.
Whatever creates such a tag must also verify each of that component's
downstream publish workflows actually started and succeeded before reporting
success; a tag existing is not evidence its publish happened.
