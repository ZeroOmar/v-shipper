"""Configuration management for v-shipper."""

import os
import base64
import yaml
from pathlib import Path
from typing import Optional
from app.models import AppConfig, DockerHost, BackupPool, WebUIConfig


class ConfigManager:
    """Manages application configuration from environment variables."""
    
    def __init__(self):
        self.config: Optional[AppConfig] = None
        self.config_file_path = Path("/tmp/config.yaml")
    
    def load(self) -> AppConfig:
        """Load configuration from VOLUME_MANAGER_CONFIG environment variable."""
        config_yaml = os.getenv("VOLUME_MANAGER_CONFIG")
        
        if not config_yaml:
            raise ValueError("VOLUME_MANAGER_CONFIG environment variable not set")
        
        try:
            config_dict = yaml.safe_load(config_yaml)
            
            if not config_dict:
                raise ValueError("Configuration YAML is empty")
            
            # Parse docker_hosts
            docker_hosts = []
            for host_config in config_dict.get("docker_hosts", []):
                host = DockerHost(
                    name=host_config["name"],
                    pool=host_config["pool"],
                    pool_type=host_config.get("pool_type", "local"),
                    remote_host=host_config.get("remote_host"),
                    rsync_module=host_config.get("rsync_module")
                )
                docker_hosts.append(host)
            
            # Parse backup_pools
            backup_pools = []
            for backup_config in config_dict.get("backup_pools", []):
                backup = BackupPool(
                    name=backup_config["name"],
                    pool=backup_config.get("pool") or backup_config.get("path"),
                    pool_type=backup_config.get("pool_type", "local"),
                    remote_host=backup_config.get("remote_host"),
                    rsync_module=backup_config.get("rsync_module")
                )
                backup_pools.append(backup)
            
            # Parse web_ui
            web_ui_config = config_dict.get("web_ui", {})
            web_ui = WebUIConfig(
                port=web_ui_config.get("port", 80),
                admin_user=web_ui_config.get("admin_user", "admin"),
                admin_password=web_ui_config.get("admin_password", "")
            )
            
            self.config = AppConfig(
                docker_hosts=docker_hosts,
                backup_pools=backup_pools,
                web_ui=web_ui,
                staging_dir=config_dict.get("staging_dir", "/tmp/staging")
            )
            
            # Save to /tmp/config.yaml for reference
            self._save_config_to_file()
            
            print(f"[CONFIG] Loaded configuration: {len(docker_hosts)} docker hosts, "
                  f"{len(backup_pools)} backup pools", flush=True)
            
            return self.config
        
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML configuration: {e}")
        except Exception as e:
            raise ValueError(f"Configuration loading error: {e}")
    
    def _save_config_to_file(self):
        """Save configuration to /tmp/config.yaml for debugging."""
        try:
            with open(self.config_file_path, 'w') as f:
                config_dict = {
                    "docker_hosts": [h.model_dump() for h in self.config.docker_hosts],
                    "backup_pools": [b.model_dump() for b in self.config.backup_pools],
                    "web_ui": self.config.web_ui.model_dump()
                }
                yaml.dump(config_dict, f)
        except Exception as e:
            print(f"[WARNING] Failed to save config file: {e}", flush=True)
    
    def get(self) -> AppConfig:
        """Get loaded configuration."""
        if not self.config:
            return self.load()
        return self.config
    
    def validate_auth(self, username: str, password: str) -> bool:
        """Validate username and password."""
        if not self.config:
            return False

        try:
            try:
                config_password = base64.b64decode(self.config.web_ui.admin_password).decode('utf-8')
            except Exception:
                config_password = self.config.web_ui.admin_password

            return username == self.config.web_ui.admin_user and password == config_password
        except Exception as e:
            print(f"[ERROR] Auth validation error: {e}", flush=True)
            return False


# Global configuration instance
_config_manager = ConfigManager()


def get_config() -> AppConfig:
    """Get application configuration."""
    return _config_manager.get()


def load_config() -> AppConfig:
    """Load application configuration."""
    return _config_manager.load()


def validate_auth(username: str, password: str) -> bool:
    """Validate authentication credentials."""
    return _config_manager.validate_auth(username, password)
