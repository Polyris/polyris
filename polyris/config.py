"""
Centralized configuration for polyris.

Configuration is loaded from (in priority order):
1. CLI arguments  (--stage, --stack, --profile, --namespace)
2. Environment variables  (POLYRIS_NAMESPACE, POLYRIS_STAGE, POLYRIS_STACK, POLYRIS_REGION)
3. config.py in project root  (ENVIRONMENTS dict)

Example config.py in your pipelines repo:

    # The dict KEYS ("dev", "prod") are the stage names — that's what
    # --stage picks. Everything inside a stage's dict is the environment
    # config for that stage.
    ENVIRONMENTS = {
        "dev": {
            "stack_name": "polyris-dev",   # SAM stack name (matches samconfig.toml)
            "namespace": "acme",
            "region": "us-east-1",
            "profile": "my-dev-profile",   # optional
            "account_id": "123456789012",  # optional guard
            "roles": {
                "etl": "arn:aws:iam::123456789012:role/etl-role",
            },
        },
        "prod": {
            "stack_name": "polyris-prod",
            "namespace": "acme",
            "region": "us-east-1",
        },
    }

    DEFAULT_STAGE = "dev"

Usage:
    from polyris.config import config

    print(config.namespace)
    print(config.stage)
    print(config.stack_name)
    print(config.profile)
    role = config.roles["etl"]

See docs/reference/CONFIGURATION.md for details.
"""

import ast
import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _has_environments_assignment(config_path: Path) -> bool:
    """Return True if config_path has a top-level ENVIRONMENTS assignment.

    Uses ast.parse — never executes the file.  SyntaxError propagates so the
    caller can surface it (ADR #38); OSError (unreadable file) returns False.

    Recognises plain assignment (``ENVIRONMENTS = ...``), annotated assignment
    (``ENVIRONMENTS: dict = ...``), and tuple unpacking (``X, ENVIRONMENTS = ...``).
    """
    try:
        source = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    tree = ast.parse(source, filename=str(config_path))

    def _names_environments(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "ENVIRONMENTS"
        if isinstance(node, ast.Tuple):
            return any(isinstance(elt, ast.Name) and elt.id == "ENVIRONMENTS" for elt in node.elts)
        return False

    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(_names_environments(t) for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if _names_environments(node.target):
                return True
    return False


def _exec_config_module(config_path: Path):
    """Import a ``config.py`` file as a module.

    Raises on failure (the caller decides how loud to be) and never returns a
    half-initialised module. Guarding the ``spec``/``loader`` ``None`` case keeps
    a missing or unreadable file from failing with an opaque ``AttributeError``.
    """
    spec = importlib.util.spec_from_file_location("_polyris_config", config_path)
    if spec is None or spec.loader is None:  # pragma: no cover -- spec_from_file_location only returns None for pathological paths; guarded for a clear error rather than an opaque AttributeError
        raise ImportError(f"cannot load module spec for {config_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_project_config() -> Optional[Path]:
    """Find config.py in current directory or parents.

    Uses ast.parse to check for ENVIRONMENTS — never executes files during
    discovery.  Stops walking at a project boundary (.git, pyproject.toml) so
    unrelated config.py files above the project root are never read.

    POLYRIS_CONFIG env var skips the walk but still sniffs the pointed-to file
    so that a file without ENVIRONMENTS is never silently executed.
    """
    env_path = os.environ.get("POLYRIS_CONFIG")
    if env_path:
        p = Path(env_path)
        if not p.exists():
            return None
        try:
            if not _has_environments_assignment(p):
                print(f"⚠️  POLYRIS_CONFIG={p} has no ENVIRONMENTS assignment — skipping")
                return None
        except SyntaxError as e:
            print(f"⚠️  Skipping {p}: syntax error ({e})")
            return None
        return p

    current = Path.cwd()
    for directory in [current, *current.parents]:
        config_path = directory / "config.py"
        if config_path.exists():
            try:
                if _has_environments_assignment(config_path):
                    return config_path
            except SyntaxError as e:
                print(f"⚠️  Skipping {config_path}: syntax error ({e})")
        # Stop at project boundary — don't read files outside this project
        if (directory / ".git").exists() or (directory / "pyproject.toml").exists():
            break
    return None


def _load_project_config() -> Dict[str, Any]:
    """Load ENVIRONMENTS from config.py."""
    config_path = _find_project_config()
    if not config_path:
        return {}

    try:
        mod = _exec_config_module(config_path)
    except Exception as e:  # pragma: no cover -- _find_project_config already imported this same file successfully, so a re-import here cannot realistically fail
        # Surface the failure (ADR #38) instead of silently using defaults —
        # a broken config.py otherwise produces confusing downstream errors.
        print(f"⚠️  Failed to load config from {config_path}: {e}")
        return {}
    return {
        "environments": getattr(mod, "ENVIRONMENTS", {}),
        "default_stage": getattr(mod, "DEFAULT_STAGE", "dev"),
    }


class _RolesDict(dict):
    """Dictionary for roles with environment variable override support."""

    def __getitem__(self, key: str) -> str:
        env_var = f"POLYRIS_ROLE_{key.upper()}"
        env_value = os.environ.get(env_var)
        if env_value:
            return env_value
        if key in self.keys():
            return super().__getitem__(key)
        raise KeyError(
            f"Role '{key}' not configured.\n"
            f"Set {env_var} env var, or add to config.py:\n"
            f"  ENVIRONMENTS = {{\n"
            f"      \"<stage>\": {{\n"
            f"          \"roles\": {{\"{key}\": \"arn:aws:iam::ACCOUNT:role/ROLE_NAME\"}}\n"
            f"      }}\n"
            f"  }}"
        )

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        env_var = f"POLYRIS_ROLE_{key.upper()}"
        if os.environ.get(env_var):
            return True
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:  # match dict/Mapping LSP
        try:
            return self[key]
        except KeyError:
            return default


class PolyrisConfig:
    """
    Central configuration for polyris.

    Reads from config.py ENVIRONMENTS dict with env var overrides.

    Attributes:
        namespace: Project namespace for resource naming
        stage: Deployment stage (dev, prod, etc.)
        region: AWS region
        profile: AWS profile (optional)
        roles: Dictionary of cross-account role ARNs
    """

    _instance = None
    _loaded = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_environments"):
            self._environments: Dict[str, Any] = {}
            self._default_stage: str = "dev"

    def _ensure_loaded(self) -> None:
        if not PolyrisConfig._loaded:
            self._load()
            PolyrisConfig._loaded = True

    def _load(self) -> None:
        project = _load_project_config()
        self._environments = project.get("environments", {})
        self._default_stage = project.get("default_stage", "dev")

    def _env_config(self, stage: Optional[str] = None) -> Dict[str, Any]:
        """Get config for a specific stage."""
        self._ensure_loaded()
        s = stage or self.stage
        return self._environments.get(s, {})

    @property
    def namespace(self) -> str:
        env_value = os.environ.get("POLYRIS_NAMESPACE")
        if env_value:
            return env_value
        return self._env_config().get("namespace", "polyris")

    @property
    def stage(self) -> str:
        env_value = os.environ.get("POLYRIS_STAGE")
        if env_value:
            return env_value
        self._ensure_loaded()
        return self._default_stage

    @property
    def region(self) -> str:
        env_value = os.environ.get("POLYRIS_REGION") or os.environ.get("AWS_REGION")
        if env_value:
            return env_value
        return self._env_config().get("region", "us-east-1")

    @property
    def profile(self) -> Optional[str]:
        env_value = os.environ.get("POLYRIS_PROFILE") or os.environ.get("AWS_PROFILE")
        if env_value:
            return env_value
        return self._env_config().get("profile", None)

    @property
    def stack_name(self) -> Optional[str]:
        env_value = os.environ.get("POLYRIS_STACK") or os.environ.get("POLYRIS_STACK_NAME")
        if env_value:
            return env_value
        return self._env_config().get("stack_name", None)

    @property
    def roles(self) -> _RolesDict:
        return _RolesDict(self._env_config().get("roles", {}))

    def for_stage(self, stage: str) -> "_StageConfig":  # duck-typed config view
        """Get config view for a specific stage (used by polyris-deploy --stage)."""
        view = _StageConfig(self, stage)
        return view

    def get(self, key: str, default: Any = None) -> Any:
        env_var = f"POLYRIS_{key.upper()}"
        env_value = os.environ.get(env_var)
        if env_value:
            return env_value
        return self._env_config().get(key, default)

    def reload(self) -> None:
        self._load()
        PolyrisConfig._loaded = True

    def reset(self) -> None:
        PolyrisConfig._loaded = False
        self._environments = {}
        self._default_stage = "dev"


class _StageConfig:
    """Config view for a specific stage — used by polyris-deploy --stage."""

    def __init__(self, parent: PolyrisConfig, stage: str):
        self._parent = parent
        self._stage = stage

    def _env_config(self) -> Dict[str, Any]:
        envs = self._parent._environments
        if envs and self._stage not in envs:
            configured = sorted(envs.keys())
            raise ValueError(
                f"Unknown stage {self._stage!r}. "
                f"Configured stages: {configured}. "
                f"Check --stage or DEFAULT_STAGE in config.py."
            )
        return envs.get(self._stage, {})

    @property
    def namespace(self) -> str:
        return os.environ.get("POLYRIS_NAMESPACE") or self._env_config().get("namespace", "polyris")

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def region(self) -> str:
        return (os.environ.get("POLYRIS_REGION") or os.environ.get("AWS_REGION")
                or self._env_config().get("region", "us-east-1"))

    @property
    def profile(self) -> Optional[str]:
        return (os.environ.get("POLYRIS_PROFILE") or os.environ.get("AWS_PROFILE")
                or self._env_config().get("profile", None))

    @property
    def stack_name(self) -> Optional[str]:
        return (os.environ.get("POLYRIS_STACK") or os.environ.get("POLYRIS_STACK_NAME")
                or self._env_config().get("stack_name", None))

    @property
    def roles(self) -> _RolesDict:
        return _RolesDict(self._env_config().get("roles", {}))

    def get(self, key: str, default: Any = None) -> Any:
        env_var = f"POLYRIS_{key.upper()}"
        return os.environ.get(env_var) or self._env_config().get(key, default)


# Singleton instance
config = PolyrisConfig()
