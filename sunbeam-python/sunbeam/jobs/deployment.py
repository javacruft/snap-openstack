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
from typing import Type

import pydantic
from juju.controller import Controller

from sunbeam.clusterd.client import Client
from sunbeam.jobs.juju import JujuAccount, JujuController

LOG = logging.getLogger(__name__)
PROXY_CONFIG_KEY = "ProxySettings"

_cls_registry: dict[str, Type["Deployment"]] = {}


def register_deployment_type(type_: str, cls: Type["Deployment"]):
    global _cls_registry
    _cls_registry[type_] = cls


def get_deployment_class(type_: str) -> Type["Deployment"]:
    global _cls_registry
    return _cls_registry[type_]


class Deployment(pydantic.BaseModel):
    name: str
    url: str
    type: str
    juju_account: JujuAccount | None = None
    juju_controller: JujuController | None = None

    @property
    def infrastructure_model(self) -> str:
        """Return the infrastructure model name."""
        return NotImplemented

    @classmethod
    def load(cls, deployment: dict) -> "Deployment":
        """Load deployment from dict."""
        if type_ := deployment.get("type"):
            return _cls_registry.get(type_, Deployment)(**deployment)
        raise ValueError("Deployment type not set.")

    @classmethod
    def import_step(cls) -> Type:
        """Return a step for importing a deployment.

        This step will be used to make sure the deployment is valid.
        The step must take as constructor arguments: DeploymentsConfig, Deployment.
        The Deployment must be of the type that the step is registered for.
        """
        return NotImplemented  # type: ignore

    def get_client(self) -> Client:
        """Return a client instance"""
        return NotImplemented  # type: ignore

    def get_clusterd_http_address(self) -> str:
        """Return the address of the clusterd server."""
        return NotImplemented  # type: ignore

    def get_connected_controller(self) -> Controller:
        """Return connected controller."""
        if self.juju_account is None:
            raise ValueError(f"No juju account configured for deployment {self.name}.")
        if self.juju_controller is None:
            raise ValueError(
                f"No juju controller configured for deployment {self.name}."
            )
        return self.juju_controller.to_controller(self.juju_account)

    def generate_preseed(self, console) -> str:
        """Generate preseed for deployment."""
        return NotImplemented

    def get_default_proxy_settings(self) -> dict:
        """Return default proxy settings."""
        return {}
