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

import logging

from sunbeam.clusterd.client import Client
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import Manifest
from sunbeam.jobs.steps import (
    AddMachineUnitsStep,
    DeployMachineApplicationStep,
    RemoveMachineUnitStep,
)

LOG = logging.getLogger(__name__)
CONFIG_KEY = "TerraformVarsSunbeamMachine"
APPLICATION = "sunbeam-machine"
SUNBEAM_MACHINE_APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
SUNBEAM_MACHINE_UNIT_TIMEOUT = (
    1200  # 20 minutes, adding / removing units can take a long time
)


class DeploySunbeamMachineApplicationStep(DeployMachineApplicationStep):
    """Deploy openstack-hyervisor application using Terraform cloud"""

    def __init__(
        self,
        client: Client,
        manifest: Manifest,
        jhelper: JujuHelper,
        model: str,
        refresh: bool = False,
        proxy_settings: dict = {},
    ):
        super().__init__(
            client,
            manifest,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "sunbeam-machine-plan",
            "Deploy sunbeam-machine",
            "Deploying Sunbeam Machine",
            refresh,
        )
        self.proxy_settings = proxy_settings

    def get_application_timeout(self) -> int:
        return SUNBEAM_MACHINE_APP_TIMEOUT

    def extra_tfvars(self) -> dict:
        return {
            "charm_config": {
                "http_proxy": self.proxy_settings.get("HTTP_PROXY", ""),
                "https_proxy": self.proxy_settings.get("HTTPS_PROXY", ""),
                "no_proxy": self.proxy_settings.get("NO_PROXY", ""),
            }
        }


class AddSunbeamMachineUnitsStep(AddMachineUnitsStep):
    """Add Sunbeam machine Units."""

    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        model: str,
    ):
        super().__init__(
            client,
            names,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Add Sunbeam-machine unit(s)",
            "Adding Sunbeam Machine unit to machine(s)",
        )

    def get_unit_timeout(self) -> int:
        return SUNBEAM_MACHINE_UNIT_TIMEOUT


class RemoveSunbeamMachineStep(RemoveMachineUnitStep):
    """Remove Sunbeam machine Unit."""

    def __init__(self, client: Client, name: str, jhelper: JujuHelper, model: str):
        super().__init__(
            client,
            name,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Remove sunbeam-machine unit",
            f"Removing sunbeam-machine unit from machine {name}",
        )

    def get_unit_timeout(self) -> int:
        return SUNBEAM_MACHINE_UNIT_TIMEOUT
