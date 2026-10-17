# Releasing

1. Bump `version` in `pyproject.toml`, run `uv lock`, and add a `CHANGELOG.md` section; merge that PR.
2. Tag the merge commit `v<version>` (for example `v0.2.0`) and push the tag.
3. `.github/workflows/release.yml` checks the tag matches `pyproject.toml`, runs lint, mypy and the tests, then pushes
   `ghcr.io/moellere/print-warden:v<version>` and `:latest`.

The first published image is private by default on GHCR: make it public (or log the Docker host in with a token
that has `read:packages`) under the repository's Packages settings.
