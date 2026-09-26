# Releasing

Releases are built and published by [`.github/workflows/release.yml`](.github/workflows/release.yml) when a version
tag is pushed.

## Cutting a release

1. Set the new version in `pyproject.toml`, move the `CHANGELOG.md` entries under it, and commit.
2. Tag the commit and push the tag:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. The workflow then:
   - checks that the tag matches the version in `pyproject.toml`,
   - builds the helper on a macOS runner (`helper/build.sh`), then the wheel (`macosx_11_0_arm64`, helper included)
     and the sdist,
   - publishes both to PyPI through trusted publishing (no API token),
   - creates a GitHub release with the wheel, the sdist, the standalone helper dylib, and `SHA256SUMS`.

If the PyPI step fails, fix the cause and re-run the failed jobs from the Actions tab; don't move the tag.

## One-time setup

- **PyPI:** in your PyPI account, go to Publishing and add a pending publisher (GitHub) with project `imbridge`,
  owner `christianblandford`, repository `imbridge`, workflow `release.yml`, and environment `pypi`. After the first
  release it becomes the project's normal trusted publisher.
- **GitHub:** the `pypi` environment is created automatically on the first run. To require a click before anything
  is published, add yourself as a required reviewer under Settings > Environments > pypi.

## Updating the helper

To move to a newer BlueBubbles helper, change `HELPER_COMMIT` in `helper/UPSTREAM`, run `helper/build.sh`, and fix
any patch that no longer applies (`git apply` stops at the first one that fails). Then run `imbridge doctor` and a
live send, reply and tapback test on a real Mac before releasing.
