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

import click
from packaging.version import Version
from rich.console import Console

from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.manifest import CharmManifest, SoftwareConfig
from sunbeam.plugins.interface.v1.openstack import (
    ApplicationChannelData,
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)
from sunbeam.versions import OPENSTACK_CHANNEL

LOG = logging.getLogger(__name__)
console = Console()


class ImagesSyncPlugin(OpenStackControlPlanePlugin):
    version = Version("0.0.1")

    def __init__(self, deployment: Deployment) -> None:
        super().__init__(
            "images-sync",
            deployment,
            tf_plan_location=TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO,
        )

    def manifest_defaults(self) -> SoftwareConfig:
        """Plugin software configuration"""
        return SoftwareConfig(
            charms={
                "openstack-images-sync-k8s": CharmManifest(channel=OPENSTACK_CHANNEL),
            }
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan: {
                "charms": {
                    "openstack-images-sync-k8s": {
                        "channel": "images-sync-channel",
                        "revision": "images-sync-revision",
                        "config": "images-sync-config",
                    }
                }
            }
        }

    def set_application_names(self) -> list:
        """Application names handled by the terraform plan."""
        return ["images-sync"]

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        return {
            "enable-images-sync": True,
        }

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {"enable-images-sync": False}

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    @click.command()
    def enable_plugin(self) -> None:
        """Enable images-sync service."""
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        """Disable images-sync service."""
        super().disable_plugin()

    @property
    def k8s_application_data(self):
        return {
            "images-sync": ApplicationChannelData(
                channel=OPENSTACK_CHANNEL,
                tfvars_channel_var=None,
            ),
        }

    @property
    def tfvars_channel_var(self):
        return "images-sync-channel"
