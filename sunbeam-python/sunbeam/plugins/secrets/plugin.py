# Copyright (c) 2023 Canonical Ltd.
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

import click
from packaging.version import Version

from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.jobs.common import read_config
from sunbeam.plugins.interface.v1.base import PluginRequirement
from sunbeam.plugins.interface.v1.openstack import (
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)
from sunbeam.plugins.vault.plugin import VaultPlugin


class SecretsPlugin(OpenStackControlPlanePlugin):
    version = Version("0.0.1")

    requires = {PluginRequirement("vault")}

    def __init__(self) -> None:
        super().__init__(
            name="secrets",
            tf_plan_location=TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO,
        )

    def set_application_names(self) -> list:
        """Application names handled by the terraform plan."""
        apps = ["barbican", "barbican-mysql-router"]
        if self.get_database_topology() == "multi":
            apps.append("barbican-mysql")

        return apps

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        return {
            "enable-barbican": True,
            "barbican-channel": "2023.2/candidate",
        }

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {"enable-barbican": False}

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def pre_enable(self) -> None:
        """Check Vault is deployed"""
        super().pre_enable()
        # TODO(gboutry): Remove this when plugin dependency is implemented
        try:
            vault_info = read_config(self.client, VaultPlugin().plugin_key)
            enabled = vault_info.get("enabled", False)
            if enabled == "false":
                raise ValueError("Vault plugin is not enabled")
        except (ConfigItemNotFoundException, ValueError) as e:
            raise click.ClickException(
                "OpenStack Secrets plugin requires Vault plugin to be enabled"
            ) from e

    @click.command()
    def enable_plugin(self) -> None:
        """Enable OpenStack Secrets service."""
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        """Disable OpenStack Secrets service."""
        super().disable_plugin()
