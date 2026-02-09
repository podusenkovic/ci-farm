"""Configuration management for CI Farm."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_FILENAME = ".ci-farm.yaml"
GLOBAL_CONFIG_PATH = Path.home() / CONFIG_FILENAME


@dataclass
class SlaveConfig:
    """Configuration for a single slave device."""

    name: str
    host: str
    user: str
    port: int = 22
    key: Optional[str] = None
    password: Optional[str] = None
    build_dir: str = "/tmp/ci-farm-builds"

    def __post_init__(self):
        if self.key:
            self.key = str(Path(self.key).expanduser())


@dataclass
class ProjectConfig:
    """Project-specific CI configuration."""

    build_command: Optional[str] = None
    pre_sync: list[str] = field(default_factory=list)
    post_build: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: [
        ".git",
        "__pycache__",
        "*.pyc",
        "node_modules",
        ".venv",
        "venv",
        ".env",
        "*.egg-info",
        "dist",
        "build",
        ".pytest_cache",
        ".ruff_cache",
    ])
    timeout: int = 3600


@dataclass
class Config:
    """Main configuration container."""

    slaves: list[SlaveConfig] = field(default_factory=list)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    default_slave: Optional[str] = None

    @classmethod
    def load(cls, project_path: Optional[Path] = None) -> "Config":
        """Load configuration from global and project-specific files."""
        config_data: dict = {}

        if GLOBAL_CONFIG_PATH.exists():
            with open(GLOBAL_CONFIG_PATH) as f:
                config_data = yaml.safe_load(f) or {}

        if project_path:
            project_config_path = project_path / CONFIG_FILENAME
            if project_config_path.exists():
                with open(project_config_path) as f:
                    project_data = yaml.safe_load(f) or {}
                    config_data = cls._merge_configs(config_data, project_data)

        return cls._from_dict(config_data)

    @classmethod
    def _merge_configs(cls, base: dict, override: dict) -> dict:
        """Merge two config dictionaries, with override taking precedence."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = cls._merge_configs(result[key], value)
            else:
                result[key] = value
        return result

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        """Create Config from dictionary."""
        slaves = [
            SlaveConfig(**slave_data)
            for slave_data in data.get("slaves", [])
        ]

        project_data = data.get("project", {})
        project = ProjectConfig(
            build_command=project_data.get("build_command"),
            pre_sync=project_data.get("pre_sync", []),
            post_build=project_data.get("post_build", []),
            exclude=project_data.get("exclude", ProjectConfig().exclude),
            timeout=project_data.get("timeout", 3600),
        )

        return cls(
            slaves=slaves,
            project=project,
            default_slave=data.get("default_slave"),
        )

    def get_slave(self, name: Optional[str] = None) -> Optional[SlaveConfig]:
        """Get slave by name or return default/first available."""
        if not self.slaves:
            return None

        if name:
            for slave in self.slaves:
                if slave.name == name:
                    return slave
            return None

        if self.default_slave:
            return self.get_slave(self.default_slave)

        return self.slaves[0]

    def save_global(self) -> None:
        """Save current configuration to global config file."""
        data = {
            "slaves": [
                {
                    "name": s.name,
                    "host": s.host,
                    "user": s.user,
                    "port": s.port,
                    "key": s.key,
                    "build_dir": s.build_dir,
                }
                for s in self.slaves
            ],
            "default_slave": self.default_slave,
        }
        with open(GLOBAL_CONFIG_PATH, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
