# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import shutil
import tempfile
from pathlib import Path

import pydantic
import yaml
from snaphelpers import Snap

from sunbeam.jobs.common import SHARE_PATH
from sunbeam.jobs.deployment import Deployment

LOG = logging.getLogger(__name__)
DEPLOYMENTS_CONFIG = SHARE_PATH / "deployments.yaml"


class DeploymentsConfig(pydantic.BaseModel):
    active: str | None = None
    deployments: list[Deployment] = []
    _path: Path | None = pydantic.PrivateAttr(default=None)

    @pydantic.validator("deployments", pre=True, each_item=True)
    def _validate_deployments(cls, deployment: dict | Deployment) -> Deployment:
        if isinstance(deployment, Deployment):
            return deployment
        if isinstance(deployment, dict):
            return Deployment.load(deployment)
        raise ValueError(f"Invalid deployment {deployment}.")

    @classmethod
    def load(cls, path: Path) -> "DeploymentsConfig":
        """Load deployment configuration from file."""
        LOG.debug(f"Loading deployment configuration from {str(path)!r}")
        with path.open() as fd:
            data = yaml.safe_load(fd)
        if data is None:
            config = cls()
        elif not isinstance(data, dict):
            raise ValueError(
                f"{str(path)} is corrupted, delete it or restore from back-up."
            )
        else:
            config = cls(**data)
        config._path = path
        return config

    def write(self):
        """Write deployment configuration to file.

        Writing to temporary file first in case there's an error during write.
        Not to lose the original file.
        """
        self_dict = self.dict()
        # self_dict has deployments with Deployment dict but not of provider
        # so workaround to add each deployment based on provider
        deployments = [d.dict() for d in self.deployments]
        self_dict["deployments"] = deployments
        LOG.debug(f"Writing deployment configuration to {str(self.path)!r}")
        with tempfile.NamedTemporaryFile("w") as tmp:
            yaml.safe_dump(self_dict, tmp)
            tmp.flush()
            shutil.copy(tmp.name, self.path)
        self.path.chmod(0o600)

    @property
    def path(self) -> Path:
        """Get path to deployment configuration."""
        if self._path is None:
            raise ValueError("Path not set.")
        return self._path

    @path.setter
    def set_path(self, path: Path):
        """Configure path for deployment configuration."""
        self._path = path

    def get_deployment(self, name: str) -> Deployment:
        """Get deployment."""
        for deployment in self.deployments:
            if deployment.name == name:
                return deployment
        raise ValueError(f"Deployment {name} not found in deployments.")

    def get_active(self) -> Deployment:
        """Get active deployment."""
        active = self.active
        if not active:
            raise ValueError("No active deployment found.")
        try:
            return self.get_deployment(active)
        except ValueError as e:
            raise ValueError(
                f"Active deployment {active} not found in configuration."
            ) from e

    def add_deployment(self, deployment: Deployment) -> None:
        """Add a deployment to configuration."""
        existing_deployment = None
        try:
            existing_deployment = self.get_deployment(deployment.name)
        except ValueError:
            # deployment does not exist
            pass
        if existing_deployment is not None:
            raise ValueError(f"Deployment {deployment.name} already exists.")
        self.deployments.append(deployment)
        self.active = deployment.name
        self.write()

    def update_deployment(self, deployment: Deployment) -> None:
        """Update deployment in configuration."""
        for i, dep in enumerate(self.deployments):
            if dep.name == deployment.name:
                self.deployments[i] = deployment
                break
        else:
            raise ValueError(f"Deployment {deployment.name} not found in deployments.")
        self.write()

    def switch(self, name: str) -> None:
        """Switch active deployment."""
        if self.active == name:
            return
        for deployment in self.deployments:
            if deployment.name == name:
                break
        else:
            raise ValueError(f"Deployment {name} not found in deployments.")
        self.active = name
        self.write()


def deployment_path(snap: Snap) -> Path:
    """Path to deployments configuration."""
    openstack = snap.paths.real_home / SHARE_PATH
    openstack.mkdir(parents=True, exist_ok=True)
    path = snap.paths.real_home / DEPLOYMENTS_CONFIG
    if not path.exists():
        path.touch(0o600)
        path.write_text("{}")
    return path


def store_deployment_as_yaml(snap: Snap, deployment: Deployment) -> Path:
    """Store a deployment as YAML and return the path."""
    openstack = snap.paths.real_home / SHARE_PATH
    openstack.mkdir(parents=True, exist_ok=True)
    path = openstack / (deployment.name + ".yaml")
    path.write_text(yaml.safe_dump(deployment.dict()))
    path.chmod(0o600)
    return path


def list_deployments(config: DeploymentsConfig) -> dict:
    deployments = [
        {
            key: value
            for key, value in deployment.dict().items()
            if key in ["name", "url", "type"]
        }
        for deployment in config.deployments
    ]
    return {"active": config.active, "deployments": deployments}
