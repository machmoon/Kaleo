# Releasing Silkscreen

Silkscreen ships three things: a **Python distribution** (sdist + wheel), a
**prebuilt web bundle** (so the review UI runs without a Node toolchain), and a
**container image** on GitHub Container Registry. All three come out of a single
version tag; nothing is built by hand.

The Ada Tauri host is not a release artifact yet. It runs from a checkout with
a local `.venv`; keep bundling disabled until the Python sidecar, signing, and
notarization pipeline are complete.

The repository is `github.com/machmoon/silkscreen`, so the image is
`ghcr.io/machmoon/silkscreen`.

---

## Cutting a release

1. **Bump the version.** `version` in `pyproject.toml` is the single source of
   truth for the three published artifacts, and no workflow writes it —
   bumping it is a deliberate commit on `main`. The tag must match:
   `pyproject.toml` at `0.2.0` is tagged `v0.2.0`.

2. **Land it on `main` and let CI go green.** `ci.yml` is the gate: ruff +
   pytest on Linux/macOS/Windows, `check_docs.py`, the Vitest + Vite `web` job,
   and a plain `docker build .`. Release workflows do not re-run the test suite,
   so a red `main` produces a broken release.

3. **Tag and push.**

   ```bash
   git fetch && git status          # the checkout goes stale fast; four people push here
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

   Prereleases use a hyphen (`v0.2.0-rc1`). Both workflows read that the same
   way: `release.yml` marks the GitHub Release as a prerelease, and `docker.yml`
   publishes the version tags but leaves `:latest` where it was.

4. **Watch the two workflows.** `release` and `docker` both fire on the tag.
   Nothing else is needed; the GitHub Release is created by the workflow.

### Undoing a bad tag

Delete the tag locally and on the remote (`git tag -d v0.2.0 && git push origin
:refs/tags/v0.2.0`) and delete the GitHub Release. Registry tags are *not*
retracted by that — republish over them with a fixed commit, or pull by the
immutable `sha-<commit>` tag instead.

---

## What the workflows do

### `.github/workflows/release.yml` — tags (`v*`) and manual dry runs

| Job | Produces |
|---|---|
| `python-dist` | `python -m build` → `dist/*.tar.gz` + `dist/*.whl`, then installs the wheel into a clean venv and imports `silkscreen` (CI only ever installs `-e .`, so this is the one check that the *built* wheel works) |
| `web-dist` | `npm ci && npm run build` in `frontend/`, zipped as `silkscreen-web-<tag>.zip` |
| `publish` | Downloads both, creates the GitHub Release with `softprops/action-gh-release@v2`, auto-generated notes, every artifact attached |

Permissions are least-privilege: the workflow default is `permissions: {}`, the
build jobs get `contents: read`, and only `publish` gets `contents: write`.

`workflow_dispatch` runs the two build jobs and skips `publish`, so the workflow
itself can be exercised without inventing a tag.

### `.github/workflows/docker.yml` — pushes to `main` and tags

Builds the root `Dockerfile` with `docker/build-push-action` and pushes to
ghcr.io using the automatic `GITHUB_TOKEN` (`permissions: packages: write`; no
long-lived registry secret exists anywhere in this repo). Tags come from
`docker/metadata-action`:

| Trigger | Tags pushed |
|---|---|
| push to `main` | `:main`, `:sha-<full-commit>` |
| tag `v1.2.3` | `:1.2.3`, `:1.2`, `:1`, `:latest`, `:sha-<full-commit>` |

`:latest` moves only on a version tag, never on a `main` push.

**Architectures.** A tag builds `linux/amd64` **and** `linux/arm64`; a `main`
push builds `linux/amd64` only. The reason is in the Dockerfile: the Node stage
is deliberately *not* pinned with `--platform=$BUILDPLATFORM`, because that
variable exists only under BuildKit and `ci.yml`'s plain `docker build .` has to
keep working on a legacy builder. Without the pin, the arm64 leg re-runs `npm
ci` and `vite build` under QEMU — a few minutes, fine to pay once per release,
not worth paying on every merge to `main`. Layer caching is `type=gha`, so
repeat builds mostly restore.

This workflow does not run on pull requests: a fork PR cannot be trusted with
registry write access, and `ci.yml`'s `docker build .` already proves the
Dockerfile builds.

### What is *not* here

There is no deploy workflow. Nothing in this repo deploys to Cloud Run, and no
GCP credentials are stored in GitHub Actions. Publishing an image is the half of
CLAUDE.md's known issue 8 that these workflows address; running one somewhere is
still a manual step.

Nothing publishes to PyPI either. The wheel is a Release asset, installed by URL
(below). Adding PyPI would mean a trusted-publisher configuration and a project
name reservation — a separate decision.

---

## Consuming each artifact

### Container image (nothing to install but Docker)

```bash
docker pull ghcr.io/machmoon/silkscreen:latest
docker run --rm -p 8080:8080 -e GOOGLE_API_KEY=... ghcr.io/machmoon/silkscreen:latest
```

Then open <http://localhost:8080> for the review UI, or `POST /generate`.
`GET /healthz` needs no key and is what the image's `HEALTHCHECK` polls — `docker
ps` shows `healthy` once the server is up.

The image runs as the unprivileged user `silkscreen` (uid 10001), listens on
`$PORT` (default 8080, which is what Cloud Run sets), and contains the built web
bundle at `/app/frontend/dist` — no Node in the runtime layer at all.

Pin by digest or by `sha-<commit>` for anything reproducible; `:latest` moves.

The image is around 740 MB. Almost all of that is dependency weight rather than
slack in the Dockerfile — OR-Tools, plus the `adk` extra's transitive
pandas/numpy/FastAPI — and the layers that hold it are `pip install`ed in one
step, so there is nothing to squash. Trimming it means dropping an extra, which
is a dependency decision, not a packaging one.

### Python distribution

Off a Release, without cloning:

```bash
pip install https://github.com/machmoon/silkscreen/releases/download/v0.2.0/silkscreen-0.2.0-py3-none-any.whl
```

The base install is the offline engine only (OR-Tools + kiutils, no network, no
API key). The model-backed and cloud paths are extras, exactly as in
`pyproject.toml`:

```bash
pip install "silkscreen[agents] @ https://.../silkscreen-0.2.0-py3-none-any.whl"
```

That gives you `python -m silkscreen "..."` plus the console scripts declared
in `[project.scripts]` (`silkscreen`, `silkscreen-serve`, `silkscreen-mcp`,
`silkscreen-review`, `silkscreen-setup`).

`release.yml` also asserts that the wheel's only top-level package is
`silkscreen`. `[tool.setuptools.packages.find]` searches `engine/`, which
contains `engine/tests` as well as `engine/silkscreen`, so without the
`include = ["silkscreen*"]` filter the wheel would drop a stray top-level
`tests` package into the installer's site-packages. `pip install -e .` — all CI
does — never reveals that; only a built distribution does, and a published wheel
is public and permanent in a way an editable install is not.

### Web bundle

`silkscreen-web-<tag>.zip` unpacks to a `dist/` directory — the same tree
`npm run build` produces. Serve it with the Python service and no Node anywhere:

```bash
unzip silkscreen-web-v0.2.0.zip -d /opt/silkscreen-web
SILKSCREEN_WEB_DIST=/opt/silkscreen-web/dist PORT=8081 python -m service.app
```

`service/app.py` serves it same-origin at `/`, which is why there are no CORS
headers and why there must never be any. (If you unzip it to `frontend/dist`
inside a checkout, the env var is unnecessary — that is the default location.)

---

## Dockerfile notes for maintainers

Things the published image needs that `ci.yml`'s `docker build .` never checks,
and which are therefore easy to regress:

- **Non-root.** The image drops to `silkscreen` (uid 10001) after the install.
  Everything under `/app` is root-owned and read-only at runtime;
  `PYTHONDONTWRITEBYTECODE=1` means nothing tries to write `.pyc` files back.
- **`HEALTHCHECK`.** Hits `/healthz` with `urllib` rather than `curl`, because
  the slim base has no curl and adding one costs an apt layer for a one-line
  request. It honours `$PORT`.
- **`.dockerignore` is not `.gitignore`.** Its patterns are root-relative, not
  recursive: a bare `node_modules/` excludes only the top-level one. The `**/`
  forms are load-bearing — a stray nested Node project under `frontend/` once
  pushed 555 MB through the build context and into the frontend stage. If a
  build suddenly gets slow, check the "Sending build context" line first.
- **Layer order.** Manifests (`package.json`/`package-lock.json`,
  `pyproject.toml`) are copied before sources so an edit to a `.py` or `.svelte`
  file does not invalidate the `npm ci` / `pip install` layers.
