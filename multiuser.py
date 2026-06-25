"""multiuser.py — per-user workspace isolation for the receipt processor.

> **Single-user stays the default and is byte-for-byte unchanged.** When
> ``MULTIUSER_ENABLED`` is off (the default), there is exactly one workspace —
> ``"default"`` — whose folders and in-memory state are the same objects the app
> has always used, so nothing about the single-user experience or the existing
> test suite changes.

## How isolation works without rewriting 400 call sites

``server.py`` reaches for module-level globals everywhere — ``_results``,
``_kanban``, ``IMAGES_FOLDER``, ``STATE_FILE`` and friends. Rather than thread a
``user_id`` through ~95 routes, the worker, the SSE layer and persistence (a huge,
error-prone diff where a *single missed scope is a privacy leak*), those globals
are replaced with thin **context proxies**. Each proxy forwards every operation
to the attribute of the *current* user's :class:`Workspace`, resolved from a
:class:`contextvars.ContextVar` that is bound:

* per HTTP request — by a global FastAPI dependency (``server._bind_workspace``),
* per worker task — from the ``user_id`` tag carried on each queue item,
* per maintenance loop — by iterating :func:`iter_workspaces`.

When nothing is bound (module import, the single-user path, most tests) the proxy
resolves to the **default** workspace, i.e. today's behaviour. Because the
default path runs *through* the proxies too, any operation a proxy fails to
forward surfaces as a loud test failure — never a silent cross-user leak.

The work queue, the SSE subscriber list, the LLM/VRAM bottleneck and its
concurrency gate stay **global and shared** (one model per box); only per-user
*data* — receipt images on disk, the board, results, reports, the run log and the
per-user form defaults — is isolated. See ``MULTIUSER.md`` for the full design.
"""
from __future__ import annotations

import contextvars
import copy as _copy
import os
import re
import threading
from pathlib import Path

# ── Feature flag & identity rules ───────────────────────────────────────────────

# Read live off this attribute (not a frozen env snapshot) so tests can flip it
# with ``monkeypatch.setattr(multiuser, "ENABLED", True)``.
ENABLED: bool = os.getenv("MULTIUSER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

# The single-user / fallback identity. Real users may never claim this id, so the
# default workspace's root (today's ``output/``) can never collide with a per-user
# root (``output/users/<id>/``).
DEFAULT_USER = "default"

# A user_id becomes part of a filesystem path, so it is the load-bearing guard for
# isolation: strict lowercase slug, no separators, no dot segments. Validated on
# every resolution; a bad id can never escape ``USERS_BASE``.
_USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_IDS = {DEFAULT_USER, "users", "admin-all", ""}


def valid_user_id(user_id: str) -> bool:
    """True only for a safe, in-charset, non-reserved identity."""
    return bool(user_id) and bool(_USER_ID_RE.match(user_id)) and user_id not in _RESERVED_IDS


def normalize_user_id(raw: str) -> str:
    """Lower-case + trim a candidate id; '' if it isn't a valid identity."""
    uid = (raw or "").strip().lower()
    return uid if valid_user_id(uid) else ""


# ── Workspace ───────────────────────────────────────────────────────────────────

class Workspace:
    """All per-user state: data folders, the crash-safe state file path, and the
    in-memory board/results/run-log containers (each with its own lock)."""

    def __init__(self, user_id: str, root: Path):
        self.user_id = user_id
        self.root = Path(root)

        # Per-user data folders (mirrors the single-user layout, rooted per user).
        self.out_folder        = self.root
        self.intake_folder     = self.root / "intake"
        self.images_folder     = self.root / "receipts"
        self.processing_folder = self.root / "processing"
        self.rejected_folder   = self.root / "unsupported"
        self.archive_folder    = self.root / "archive"
        self.state_file        = self.root / ".app_state.json"

        # In-memory runtime state (the same shapes server.py has always used).
        self.results: list[dict] = []
        self.results_lock = threading.Lock()
        self.kanban: dict[str, dict] = {}
        self.kanban_lock = threading.Lock()
        self.last_context: dict = {"employee": "Employee", "job_name": "", "job_number": ""}
        self.benchmarks: list[dict] = []
        self.bench_lock = threading.Lock()
        self.runs: list[dict] = []
        self.runs_lock = threading.Lock()
        self.current_run: dict | None = None
        self.current_run_lock = threading.Lock()
        self.run_seq = 0
        self.seen_intake: set[str] = set()
        self.seen_lock = threading.Lock()
        self.rejected_reasons: dict[str, str] = {}
        self.rejected_lock = threading.Lock()
        self.item_cache: dict[str, dict] = {}
        self.item_cache_lock = threading.Lock()
        self.status_timestamps: dict[str, float] = {}
        self.status_ts_lock = threading.Lock()
        # Sent-ledger: identity of every receipt already included in a sent report,
        # so re-adds can be skipped (with an override). ``last_report_date`` is a
        # cheap max-receipt-date watermark for an at-a-glance "already sent" hint.
        self.sent_ledger: list[dict] = []
        self.sent_ledger_lock = threading.Lock()
        self.last_report_date: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Workspace {self.user_id!r} root={self.root}>"


# ── Registry ────────────────────────────────────────────────────────────────────

# Base directory under which per-user roots live (``<output>/users/<id>``). Set by
# server.configure(); falls back to the env default so the module is importable
# standalone (and patchable in tests).
USERS_BASE: Path = Path(os.getenv("OUTPUT_FOLDER", "output")) / "users"

_default_ws: Workspace | None = None
_workspaces: dict[str, Workspace] = {}
_registry_lock = threading.RLock()


def configure(default_ws: Workspace, users_base: Path | None = None) -> None:
    """Install the default (single-user) workspace and the per-user base dir.
    Called once by server.py at import."""
    global _default_ws, USERS_BASE
    _default_ws = default_ws
    with _registry_lock:
        _workspaces[default_ws.user_id] = default_ws
    if users_base is not None:
        USERS_BASE = Path(users_base)


def default_workspace() -> Workspace:
    if _default_ws is None:  # pragma: no cover - server installs one at import
        raise RuntimeError("multiuser.configure() was never called")
    return _default_ws


def get_workspace(user_id: str) -> Workspace:
    """Resolve (creating on first use) the workspace for ``user_id``.

    The default id, an invalid id, or single-user mode all resolve to the default
    workspace — so a bad/unknown identity can never read another user's files.
    """
    if not ENABLED or user_id == DEFAULT_USER or not valid_user_id(user_id):
        return default_workspace()
    with _registry_lock:
        ws = _workspaces.get(user_id)
        if ws is None:
            # ``USERS_BASE / user_id`` with the id already charset-validated above;
            # resolve() + containment check belt-and-suspenders against traversal.
            root = (USERS_BASE / user_id).resolve()
            base = USERS_BASE.resolve()
            if base not in root.parents and root != base:
                return default_workspace()
            ws = Workspace(user_id, root)
            _workspaces[user_id] = ws
        return ws


def iter_workspaces() -> list[Workspace]:
    """Every workspace currently known (default + any per-user already created)."""
    with _registry_lock:
        return list(_workspaces.values())


def discover_user_ids() -> list[str]:
    """User ids with an existing data dir on disk (for restore-on-startup)."""
    out: list[str] = []
    try:
        base = USERS_BASE
        if base.exists():
            for p in sorted(base.iterdir()):
                if p.is_dir() and valid_user_id(p.name):
                    out.append(p.name)
    except OSError:
        pass
    return out


# ── Current-workspace binding ───────────────────────────────────────────────────

_CUR_WS: contextvars.ContextVar[Workspace | None] = contextvars.ContextVar("cur_ws", default=None)


def cur_ws() -> Workspace:
    """The workspace bound to the current request/task, else the default."""
    return _CUR_WS.get() or default_workspace()


def bind(ws: Workspace):
    """Bind ``ws`` as the current workspace; returns a token for :func:`reset`."""
    return _CUR_WS.set(ws)


def bind_user(user_id: str):
    return bind(get_workspace(user_id))


def reset(token) -> None:
    try:
        _CUR_WS.reset(token)
    except (ValueError, LookupError):  # pragma: no cover - token from another context
        pass


# ── Context proxies ─────────────────────────────────────────────────────────────
# These stand in for server.py's module-level globals so the existing call sites
# (and the existing tests that poke ``server._results`` / ``server.IMAGES_FOLDER``)
# keep working while every access is transparently scoped to ``cur_ws()``.

class _ContainerProxy:
    """Forwards list/dict/set operations to ``cur_ws().<attr>``."""
    __slots__ = ("_attr",)

    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def _t(self):
        return getattr(cur_ws(), object.__getattribute__(self, "_attr"))

    # attribute access (.append/.get/.items/.clear/.extend/.popleft/.add/.pop/…)
    def __getattr__(self, name):
        return getattr(self._t(), name)

    def __setattr__(self, name, value):
        if name == "_attr":
            object.__setattr__(self, name, value)
        else:
            setattr(self._t(), name, value)

    # mapping / sequence protocol
    def __getitem__(self, key):
        return self._t()[key]

    def __setitem__(self, key, value):
        self._t()[key] = value

    def __delitem__(self, key):
        del self._t()[key]

    def __contains__(self, key):
        return key in self._t()

    def __iter__(self):
        return iter(self._t())

    def __reversed__(self):
        return reversed(self._t())

    def __len__(self):
        return len(self._t())

    def __bool__(self):
        return bool(self._t())

    def __eq__(self, other):
        return self._t() == other

    def __ne__(self, other):
        return self._t() != other

    def __repr__(self):
        return repr(self._t())

    # copy.deepcopy(_results) etc. must copy the *target*, not the proxy.
    def __copy__(self):
        return _copy.copy(self._t())

    def __deepcopy__(self, memo):
        return _copy.deepcopy(self._t(), memo)


class _LockProxy:
    """A ``with`` context manager forwarding to ``cur_ws().<attr>`` (a real lock).
    Resolution is stable within a synchronous ``with`` block (same thread/context),
    so enter and exit always act on the same lock object."""
    __slots__ = ("_attr",)

    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def _t(self):
        return getattr(cur_ws(), object.__getattribute__(self, "_attr"))

    def __enter__(self):
        return self._t().__enter__()

    def __exit__(self, *exc):
        return self._t().__exit__(*exc)

    def acquire(self, *a, **k):
        return self._t().acquire(*a, **k)

    def release(self):
        return self._t().release()


class _PathProxy:
    """Stands in for a folder/path global, forwarding to ``cur_ws().<attr>`` (a
    real :class:`~pathlib.Path`). Supports the operators server.py uses on folders
    (``/``, ``str()``, ``os.fspath``, ``.mkdir``/``.iterdir``/``.resolve``/…)."""
    __slots__ = ("_attr",)

    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def _t(self) -> Path:
        return getattr(cur_ws(), object.__getattribute__(self, "_attr"))

    def __getattr__(self, name):
        return getattr(self._t(), name)

    def __truediv__(self, other):
        return self._t() / other

    def __rtruediv__(self, other):
        return other / self._t()

    def __fspath__(self):
        return os.fspath(self._t())

    def __str__(self):
        return str(self._t())

    def __repr__(self):
        return repr(self._t())

    def __eq__(self, other):
        return self._t() == other

    def __ne__(self, other):
        return self._t() != other

    def __hash__(self):
        return hash(self._t())


def container_proxy(attr: str) -> _ContainerProxy:
    return _ContainerProxy(attr)


def lock_proxy(attr: str) -> _LockProxy:
    return _LockProxy(attr)


def path_proxy(attr: str) -> _PathProxy:
    return _PathProxy(attr)
