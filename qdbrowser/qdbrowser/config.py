"""Configuration system for qdbrowser. TOML-backed singleton.

Mirrors qterminator/config.py — same merge semantics, same singleton
pattern (so tests can `Config._instance = None` to reset between cases).
"""

import copy
import os

try:
    import tomllib
except ImportError:  # Python <3.11
    import tomli as tomllib


CONFIG_DIR = os.path.expanduser("~/.config/qdbrowser")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")


DEFAULTS = {
    "general": {
        "window_width": 1280,
        "window_height": 800,
        "tab_position": "top",  # top, bottom, left, right
        "confirm_close": True,
        "show_menubar": False,
        "show_toolbar": True,
        "show_side_panel": True,
        "side_panel_width": 280,
        "theme_mode": "system",  # dark / light / system
        "homepage": "about:blank",
        "search_engine": "https://duckduckgo.com/?q={query}",
        "user_agent": "",  # empty = Qt default
        "save_session": True,
        "restore_session_on_start": True,
        "downloads_dir": os.path.expanduser("~/Downloads"),
    },
    "profiles": {
        "default": {
            "javascript_enabled": True,
            "images_enabled": True,
            "webgl_enabled": True,
            "plugins_enabled": True,
            "local_storage_enabled": True,
            "cookies": "allow",  # allow / block / session
            "do_not_track": False,
            "https_only": False,
            "block_third_party_cookies": True,
            "default_zoom": 1.0,
        },
    },
    "keybindings": {
        # Tabs
        "new_tab": "Ctrl+T",
        "new_window": "Ctrl+N",
        "close_tab": "Ctrl+W",
        "reopen_tab": "Ctrl+Shift+T",
        "next_tab": "Ctrl+Tab",
        "prev_tab": "Ctrl+Shift+Tab",
        "switch_to_tab_1": "Alt+1",
        "switch_to_tab_2": "Alt+2",
        "switch_to_tab_3": "Alt+3",
        "switch_to_tab_4": "Alt+4",
        "switch_to_tab_5": "Alt+5",
        "switch_to_tab_6": "Alt+6",
        "switch_to_tab_7": "Alt+7",
        "switch_to_tab_8": "Alt+8",
        "switch_to_tab_9": "Alt+9",
        "pin_tab": "Ctrl+Alt+P",
        "mute_tab": "Ctrl+M",
        # Navigation
        "address_bar": "Ctrl+L",
        "back": "Alt+Left",
        "forward": "Alt+Right",
        "reload": "Ctrl+R",
        "hard_reload": "Ctrl+Shift+R",
        "stop": "Escape",
        "home": "Alt+Home",
        # Find
        "find": "Ctrl+F",
        "find_next": "F3",
        "find_prev": "Shift+F3",
        # Splits (qterminator-style)
        "split_horizontal": "Ctrl+Shift+O",
        "split_vertical": "Ctrl+Shift+E",
        "close_split": "Ctrl+Shift+W",
        "navigate_left": "Alt+Shift+Left",
        "navigate_right": "Alt+Shift+Right",
        "navigate_up": "Alt+Shift+Up",
        "navigate_down": "Alt+Shift+Down",
        # Power user
        "command_palette": "Ctrl+E",
        "toggle_devtools": "F12",
        "view_source": "Ctrl+U",
        "fullscreen": "F11",
        "zoom_in": "Ctrl+=",
        "zoom_out": "Ctrl+-",
        "zoom_reset": "Ctrl+0",
        "reader_mode": "Ctrl+Alt+R",
        # Side panel
        "toggle_side_panel": "F4",
        "panel_bookmarks": "Ctrl+B",
        "panel_history": "Ctrl+H",
        "panel_downloads": "Ctrl+J",
        "panel_notes": "Ctrl+Alt+N",
        # Page actions
        "save_session": "Ctrl+Shift+S",
        "take_screenshot": "Ctrl+Shift+P",
        "quit": "Ctrl+Q",
    },
    "bookmarks": [],
    "blocklist": {
        "enabled": True,
        "hosts_urls": [
            # Default to no remote fetch; user supplies sources.
        ],
        "extra_blocked": [],
        "allowlist": [],
    },
    "gestures": {
        "enabled": True,
        "bindings": {
            "L": "back",
            "R": "forward",
            "U": "new_tab",
            "D": "close_tab",
            "DR": "reopen_tab",
        },
    },
    "workspaces": {},
    "sessions": {},
    "plugins": {
        "bridge_adapter": {
            # Default activation is conditional: MainWindow probes for
            # qdistro daemons when ``enabled`` is unset.
            "auto_register": True,
        },
    },
    "dark_mode": {
        "default": "auto",     # auto / always / never / contrast
        "site_overrides": {},
    },
    "translate": {
        "api_base": "https://api.openai.com/v1",
        "api_key": "",          # also read from $QDBROWSER_OPENAI_API_KEY
        "model": "gpt-4o-mini",
        "target_lang": "English",
        "timeout": 30.0,
        "max_chars": 8000,
    },
    "security": {
        # Site isolation: origins to pin to dedicated renderer processes
        # via Chromium's ``--isolate-origins`` flag. Each origin must be
        # a scheme+host (no trailing slash). The flag is composed
        # before QApplication construction in __main__.py.
        "isolate_origins": [],
        # HTTPS-only mode: rewrite http:// -> https:// for everything
        # not in ``http_allowlist`` (host-globs). When ``https_only``
        # is true and a request can't be safely upgraded (POST with
        # body, ws://, etc.) the interceptor returns an interstitial.
        "https_only": False,
        "http_allowlist": [],
        # Do-Not-Track / Global Privacy Control: when true, the
        # interceptor adds ``DNT: 1`` and ``Sec-GPC: 1`` to every
        # outbound request.
        "do_not_track": False,
        # User-agent policy: "default" lets pages override the UA via
        # JS; "strict" rejects JS-driven overrides and pins to the
        # configured / Qt-default UA.
        "user_agent_policy": "default",
        # Pin / override admin paths (overridable from env in tests).
        "cert_pins_path": "/etc/qdistro/cert-pins.json",
        "cert_pins_user_path": os.path.expanduser(
            "~/.config/qdbrowser/cert-pins.json"),
        "cert_overrides_path": "/etc/qdistro/cert-overrides.json",
        # Per-profile CA bundles. EXPERIMENTAL / UNVERIFIED. When true,
        # qdbrowser resolves a bundle named ``<profile>-ca.pem`` for the
        # profile selected at launch (``--profile``) and exports
        # ``SSL_CERT_FILE`` before QtWebEngine starts. User dir wins over
        # admin dir. Off by default (no behaviour change).
        #
        # WARNING: on an NSS-backed QtWebEngine build (the one shipped
        # here, and most distro builds) Chromium reads server-CA trust
        # from the NSS user DB (~/.pki/nssdb) and IGNORES SSL_CERT_FILE,
        # so enabling this does NOT actually change page TLS trust. To
        # really trust a private CA on such a build, import it with:
        #   certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n <name> \
        #            -i <profile>-ca.pem
        # The SSL_CERT_FILE export only takes effect on a
        # use_nss_certs=false QtWebEngine (unix system-trust verifier,
        # Chromium >=M114). See qdbrowser/ca_bundle.py for the analysis.
        # NOTE: applies per-launch only — the CA trust is process-global,
        # so a profile opened later in a running window shares the launch
        # profile's bundle.
        "per_profile_ca_bundles": False,
        "ca_bundle_user_dir": os.path.expanduser(
            "~/.config/qdbrowser/certs"),
        "ca_bundle_admin_dir": "/etc/qdistro/qdbrowser/certs",
    },
    "downloads": {
        # Master switch: when true every download lands in quarantine
        # first; when false files go straight to ``release_dir``.
        "quarantine_enabled": True,
        # Quarantine: every download lands in
        # ``~/.local/share/qdbrowser/quarantine/`` with a sibling
        # ``.qdistro-meta.json`` and an entry in ``metadata.db``.
        "quarantine_dir": os.path.expanduser(
            "~/.local/share/qdbrowser/quarantine"),
        # Optional scan command. Receives the file path as argv[1];
        # non-zero exit marks the file ``quarantined-bad``.
        "scan_command": "",
        # Target dir for release-from-quarantine.
        "release_dir": os.path.expanduser("~/Downloads"),
        # Domains whose downloads bypass quarantine entirely.  Each
        # entry is matched against the download URL's host (exact
        # match, no globbing).  Empty list = no auto-release.
        "auto_release_domains": [],
    },
    "agent_control": {
        # Master switch for method-allowlist enforcement. On by default:
        # enabling the agent socket must not also enable eval_js / type_text
        # / click_at. Admins re-enable verbs via ``allowed_methods``. See
        # the secure recipe in todo/browser/03-agent-guardrails.md.
        "policy_enforced": True,
        # Methods explicitly re-enabled by the admin. Takes precedence
        # over the built-in default-deny set (eval_js, type_text,
        # send_keys, click_at, dblclick_at, move_mouse) and over the
        # `denied_methods` list below. Only consulted when
        # ``policy_enforced = true``.
        "allowed_methods": [],
        # Extra methods the admin wants denied on top of the defaults.
        # Only consulted when ``policy_enforced = true``.
        "denied_methods": [],
        # Hostname-glob allowlist for ``rpc_navigate``. Empty = no
        # restriction. ``*.foo.com`` matches subdomains only (a real
        # dot boundary), not ``evilfoo.com``. Independent of
        # ``policy_enforced``: any non-empty allow/deny list takes
        # effect regardless.
        "navigate_allowlist": [],
        "navigate_denylist": [],
        # ---- Layer 4: rate limiting (per-client token buckets) ----
        # All limits are sliding-window counts over the trailing 60s.
        # ``0`` disables a category. Negative values are treated as 0.
        # The total bucket applies to *every* RPC including
        # screenshots/eval; the per-category buckets are additional
        # constraints. Defaults: 10 req/s (600/min), 5 screenshots/min,
        # 3 eval_js/min, 10 open_tab/min.
        "rate_limit_per_minute": 600,
        "screenshot_rate_limit_per_minute": 5,
        "eval_rate_limit_per_minute": 3,
        "open_tab_rate_limit_per_minute": 10,
        # ---- Layer 5: broker mediation ----
        # When enabled, calls to ``_DEFAULT_DENIED_METHODS`` (or any
        # method in ``broker_mediated_methods``) get a synchronous
        # D-Bus check against ``Broker.CheckAgentAction``. See
        # ``_broker_check`` for the fail-open vs fail-closed policy.
        "broker_enabled": False,
        "broker_bus_name": "org.qdistro.Broker",
        "broker_object_path": "/org/qdistro/Broker",
        "broker_interface": "org.qdistro.Broker",
        "broker_timeout_ms": 1500,
        # Methods that always require broker approval when
        # ``broker_enabled``. The default-deny set is implicitly
        # included.
        "broker_mediated_methods": [],
        # ---- Layer 6: client exe allowlist ----
        # Each entry is either an absolute path (resolved to its current
        # SHA256 at plugin activation) or ``sha256:<hex>``. Empty list
        # disables the check (UID match via SO_PEERCRED is still
        # required). Non-empty list rejects connections whose
        # ``/proc/<pid>/exe`` digest is not in the resolved set with
        # ``client_not_allowed``.
        "allowed_client_exes": [],
        # Clients must complete a handshake (``{op: "handshake",
        # exe: "...", pid: N}``) before any JSON-RPC calls. The
        # handshake is audit-only: it logs the client's claimed
        # identity and verifies /proc/<pid>/exe, but a TOCTOU racer
        # can defeat the check. On by default so a same-UID peer
        # cannot skip identification.
        "require_handshake": True,
    },
}


class Config:
    """Singleton TOML-backed config. Same shape as qterminator's."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._data = copy.deepcopy(DEFAULTS)
        self._load()
        self._loaded = True

    def _load(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "rb") as f:
                user_config = tomllib.load(f)
            self._merge(self._data, user_config)

    def _merge(self, base, override):
        for key, value in override.items():
            if (key in base and isinstance(base[key], dict)
                    and isinstance(value, dict)):
                self._merge(base[key], value)
            else:
                base[key] = value

    def get(self, *keys, default=None):
        node = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def set(self, *keys_and_value):
        if len(keys_and_value) < 2:
            return
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        node = self._data
        for key in keys[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value

    def get_profile(self, name="default"):
        profiles = self._data.get("profiles", {})
        profile = copy.deepcopy(DEFAULTS["profiles"]["default"])
        if name in profiles:
            profile.update(profiles[name])
        return profile

    def get_keybinding(self, action):
        return self._data.get("keybindings", {}).get(action)

    @property
    def general(self):
        return self._data.get("general", {})

    @property
    def keybindings(self):
        return self._data.get("keybindings", {})

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            _write_toml(f, self._data)


def _write_toml(f, data, prefix=""):
    simple = {}
    tables = {}
    array_tables = {}  # name -> list of dicts (arrays of tables)
    for k, v in data.items():
        if isinstance(v, dict):
            tables[k] = v
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            array_tables[k] = v
        else:
            simple[k] = v
    for k, v in simple.items():
        f.write(f"{_toml_key(k)} = {_toml_value(v)}\n")
    for k, v in tables.items():
        section = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        f.write(f"\n[{section}]\n")
        _write_toml(f, v, section)
    for k, items in array_tables.items():
        section = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        for item in items:
            f.write(f"\n[[{section}]]\n")
            _write_toml(f, item, section)


def _toml_string(s):
    if "'" not in s and "\n" not in s and "\r" not in s and "\t" not in s:
        return f"'{s}'"
    escaped = (s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t"))
    return f'"{escaped}"'


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return _toml_string(v)
    if isinstance(v, list):
        # Lists of dicts are written as ``[[name]]`` arrays-of-tables
        # by ``_write_toml``, not as a one-line value — but tests that
        # call ``_toml_value`` directly still need a sane string. Emit
        # inline-table syntax so it round-trips through ``tomllib``.
        if v and isinstance(v[0], dict):
            items = ", ".join(_toml_inline_table(i) for i in v)
        else:
            items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    if isinstance(v, dict):
        return _toml_inline_table(v)
    return _toml_string(str(v))


def _toml_inline_table(d: dict) -> str:
    """Render a dict as a TOML inline table ``{k = v, k = v}``."""
    parts = []
    for k, v in d.items():
        parts.append(f"{_toml_key(k)} = {_toml_value(v)}")
    return "{" + ", ".join(parts) + "}"


def _toml_key(k: str) -> str:
    """Render a TOML key. Bare keys must match ``[A-Za-z0-9_-]+``;
    anything else (e.g. a hostname with dots) needs quoting so that
    ``foo.bar = "x"`` round-trips as a single key rather than a dotted
    table path.
    """
    import re as _re
    if _re.fullmatch(r"[A-Za-z0-9_-]+", k):
        return k
    return _toml_string(k)
