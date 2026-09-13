"""The ``~/.kaleo/*.env`` files: where a desktop install keeps what Setup saved.

Three files, one per account the Setup Assistant can connect -- ``billing``
(Stripe), ``google`` (the OAuth client and the Chat webhook) and ``microsoft``
(the Entra app registration) -- each a flat ``KEY=value`` file written 0600
under a 0700 directory, and each read back at **startup, by an explicit call**
(``service.app.main`` and ``desktop/launcher.py``), never at import. A module
that reads the developer's real ``~/.kaleo`` the moment a test imports it is a
module whose tests depend on what the developer connected last week.

Precedence, stated once: a value already in the process environment always
wins over a file. :func:`apply_env` is ``setdefault`` and nothing else, so a
deployment's Secret Manager injection, a shell ``export`` or a ``.env`` the
launcher already read cannot be overridden by a file left on a laptop. Every
save reports whether the running process is actually using what it just
wrote (:func:`activation`).

Demo state is kept strictly apart. ``KALEO_SETUP_MODE=demo`` records its
pretend sign-ins under ``~/.kaleo/demo/``; those files start with a header and
carry :data:`DEMO_MARKER` (``KALEO_DEMO=1``), and the rule is enforced on
**both** sides of the boundary rather than by path alone: a live read of any
file carrying the marker is refused, a live save into ``demo/`` is refused, a
demo read of a file *without* the marker is refused, and
:func:`apply_saved_env` never opens the demo directory at all. A demo file
that leaked into the environment would make ``/integrations`` claim an
account that was never connected.

Atomicity and permissions are the same rule ``googleapps/auth.py`` and
``billing_routes`` already keep, done properly: ``tempfile.mkstemp`` creates
the temporary 0600 in the target directory and ``os.replace`` swaps it in, so
there is never a moment when a half-written or world-readable credential
exists on disk. A symlinked home, directory or target is refused outright --
following one would write a secret wherever the link points.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path

__all__ = [
    "BILLING_KEYS",
    "DEMO_HEADER",
    "DEMO_MARKER",
    "DemoFileError",
    "EnvFileError",
    "FILES",
    "activation",
    "apply_env",
    "apply_saved_env",
    "demo_dir",
    "env_path",
    "is_private",
    "kaleo_home",
    "load_env",
    "mode_hint",
    "remove_env",
    "save_env",
]

#: The key every demo file carries. Its presence, not its path, is what the
#: live side refuses.
DEMO_MARKER = "KALEO_DEMO"
DEMO_HEADER = "# KALEO DEMO STATE -- not a credential"
LIVE_HEADER = "# Written by Hardy's Setup Assistant. Never commit this file."

#: In the order ``billing_routes`` writes them; the tuple is the one
#: definition and ``billing_routes._SETTABLE`` is an alias of it.
BILLING_KEYS = (
    "STRIPE_API_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_ID",
    "STRIPE_CREDIT_MKCU",
    "KALEO_RATE_CENTS_PER_KCU",
)

#: Which keys each file may carry into the environment. A key outside its
#: file's set is dropped on read and refused on write -- a file cannot smuggle
#: ``SILKSCREEN_ACCESS_TOKEN`` or ``PATH`` into the process.
FILES: dict[str, frozenset[str]] = {
    "billing": frozenset(BILLING_KEYS),
    "google": frozenset(
        {"GOOGLEAPPS_CLIENT_ID", "GOOGLEAPPS_CLIENT_SECRET", "GOOGLEAPPS_CHAT_WEBHOOK"}
    ),
    "microsoft": frozenset({"TEAMS_APP_ID", "TEAMS_APP_SECRET", "TEAMS_TENANT_ID"}),
}

_HOME_ENV = "KALEO_HOME"
_BILLING_PATH_ENV = "KALEO_BILLING_ENV_PATH"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


class EnvFileError(RuntimeError):
    """A file could not be used safely: a symlink, a bad key, a bad name."""


class DemoFileError(EnvFileError):
    """A demo file reached the live side, or a live file reached the demo side."""


# ---------------------------------------------------------------- paths


def kaleo_home() -> Path:
    """``$KALEO_HOME`` or ``~/.kaleo``, resolved **at call time**.

    Never cached at import: the test fixture points it at ``tmp_path`` and a
    launcher may set it after the module is loaded.
    """
    override = (os.environ.get(_HOME_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kaleo"


def demo_dir() -> Path:
    return kaleo_home() / "demo"


def env_path(name: str, *, demo: bool = False) -> Path:
    """Where ``name``'s file lives.

    Live ``billing`` honours ``KALEO_BILLING_ENV_PATH``, the override
    ``billing_routes`` has always read.
    """
    if name not in FILES:
        raise EnvFileError(f"no such env file {name!r}; one of {sorted(FILES)}")
    if demo:
        return demo_dir() / f"{name}.env"
    if name == "billing":
        override = (os.environ.get(_BILLING_PATH_ENV) or "").strip()
        if override:
            return Path(override).expanduser()
    return kaleo_home() / f"{name}.env"


def _under_demo(path: Path) -> bool:
    try:
        path.resolve().relative_to(demo_dir().resolve())
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------- read


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            values[key] = value
    return values


def load_env(path: Path | str, *, demo: bool = False) -> dict[str, str]:
    """Every ``KEY=value`` in the file, or ``{}`` when it does not exist.

    Raises :class:`DemoFileError` when the file is on the wrong side of the
    demo boundary -- **whatever its path**. Raises :class:`EnvFileError` for a
    symlink. Other ``OSError``s propagate: a permission error on a credential
    file is something to report, not an empty dict.
    """
    path = Path(path)
    if path.is_symlink():
        raise EnvFileError(f"{path} is a symlink; refusing to read through it")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values = _parse(text)
    marked = values.get(DEMO_MARKER) == "1"
    if marked and not demo:
        raise DemoFileError(
            f"{path} carries {DEMO_MARKER}=1: demo state is never read as a "
            "credential"
        )
    if demo and not marked:
        raise DemoFileError(
            f"{path} does not carry {DEMO_MARKER}=1: a file without the demo "
            "marker is never read as demo state"
        )
    return values


def is_private(path: Path | str) -> bool:
    """True when the file exists and only its owner can read it.

    On Windows ``stat`` reports 0o666 for everything, so there the answer is
    "exists" -- the user's profile is what keeps it private.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return False
    if os.name == "nt":
        return True
    return mode == _FILE_MODE


def mode_hint(path: Path | str) -> str | None:
    """The ``/setup`` hint for a file that exists and is not mode 0600."""
    path = Path(path)
    if not path.exists() or is_private(path):
        return None
    return f"{path} is not mode 0600; save again from Setup to rewrite it"


# ---------------------------------------------------------------- write


def _ensure_dir(directory: Path) -> None:
    """Create ``directory`` 0700 (and its parents), refusing symlinks."""
    for candidate in (directory, *directory.parents):
        if candidate.is_symlink():
            raise EnvFileError(f"{candidate} is a symlink; refusing to write under it")
        if candidate.exists():
            break
    if not directory.exists():
        directory.mkdir(parents=True, mode=_DIR_MODE)
    if os.name != "nt":
        # mkdir honours the umask; the chmod makes the mode what was asked.
        os.chmod(directory, _DIR_MODE)


def _check_value(key: str, value: str) -> None:
    if not isinstance(value, str):
        raise EnvFileError(f"{key} must be a string")
    if "\n" in value or "\r" in value:
        # A newline in a value writes a second key on the next line.
        raise EnvFileError(f"{key} must not contain a line break")
    if not key.replace("_", "").isalnum() or key[0].isdigit():
        raise EnvFileError(f"{key!r} is not an environment variable name")


def save_env(
    path: Path | str,
    values: Mapping[str, str],
    *,
    allow: Iterable[str],
    demo: bool = False,
) -> Path:
    """Write ``values`` (allowlisted keys only) atomically, mode 0600.

    Live: refuses a path under ``demo/`` and refuses to write the marker.
    Demo: writes the header and ``KALEO_DEMO=1`` first, whatever ``values``
    say. Either way the target's directory is created 0700 and a symlink
    anywhere on the way is a refusal.
    """
    path = Path(path)
    allowed = frozenset(allow)
    if DEMO_MARKER in values:
        raise EnvFileError(
            f"{DEMO_MARKER} is written by save_env(demo=True), not by a caller"
        )
    if not demo and _under_demo(path):
        raise EnvFileError(f"live state is never written under {demo_dir()}")
    if demo and not _under_demo(path):
        raise EnvFileError(f"demo state is only written under {demo_dir()}")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise EnvFileError(f"{path.name} may not carry {', '.join(unknown)}")
    for key, value in values.items():
        _check_value(key, value)
    if path.is_symlink():
        raise EnvFileError(f"{path} is a symlink; refusing to write through it")

    _ensure_dir(path.parent)
    lines = [DEMO_HEADER if demo else LIVE_HEADER]
    if demo:
        lines.append(f"{DEMO_MARKER}=1")
    lines.extend(f"{key}={value}" for key, value in values.items() if value)
    body = "\n".join(lines) + "\n"

    # mkstemp creates the file 0600 in the target directory, so the rename
    # cannot cross a filesystem and the bytes are never world-readable.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def remove_env(path: Path | str) -> bool:
    """Delete the file. True if something was removed; a symlink is refused."""
    path = Path(path)
    if path.is_symlink():
        raise EnvFileError(f"{path} is a symlink; refusing to remove through it")
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------- environment


def apply_env(
    values: Mapping[str, str],
    *,
    allow: Iterable[str],
    environ: MutableMapping[str, str] = os.environ,
) -> int:
    """``setdefault`` each allowlisted value into ``environ``; count the ones set.

    Anything carrying :data:`DEMO_MARKER` is skipped whole -- demo state is
    never exported, so ``/integrations`` keeps saying ``unconfigured`` in demo.
    """
    if values.get(DEMO_MARKER) == "1":
        return 0
    allowed = frozenset(allow)
    applied = 0
    for key, value in values.items():
        if key not in allowed or not value:
            continue
        if not environ.get(key):
            environ[key] = value
            applied += 1
    return applied


def activation(
    values: Mapping[str, str],
    *,
    environ: Mapping[str, str] = os.environ,
) -> tuple[bool, str]:
    """Is the process using what was just saved?

    ``(True, "file")`` when every saved value is what the environment now
    holds; ``(False, "environment")`` when some variable was already set to
    something else and, by the precedence rule, still wins.
    """
    for key, value in values.items():
        if value and environ.get(key, "") != value:
            return False, "environment"
    return True, "file"


def apply_saved_env(
    environ: MutableMapping[str, str] = os.environ,
) -> dict[str, int]:
    """Read every live file under :func:`kaleo_home` into ``environ``.

    The startup call. Never opens ``demo/``; a file that cannot be read (a
    demo marker where none should be, a symlink, a permission problem) counts
    as zero for that name rather than stopping the service from starting --
    the gap is reported by ``GET /setup`` where someone can act on it.
    """
    counts: dict[str, int] = {}
    for name, allow in FILES.items():
        try:
            values = load_env(env_path(name))
        except (OSError, EnvFileError):
            counts[name] = 0
            continue
        counts[name] = apply_env(values, allow=allow, environ=environ)
    return counts
