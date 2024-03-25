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
import traceback
from typing import Optional

import openstack
from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ConfigItemNotFoundException,
    NodeNotExistInClusterException,
)
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.commands.openstack_api import guests_on_hypervisor, remove_hypervisor
from sunbeam.commands.terraform import TerraformException
from sunbeam.jobs.common import BaseStep, Result, ResultType, read_config, update_config
from sunbeam.jobs.juju import (
    ApplicationNotFoundException,
    JujuHelper,
    TimeoutException,
    run_sync,
)
from sunbeam.jobs.manifest import Manifest
from sunbeam.jobs.steps import AddMachineUnitsStep, DeployMachineApplicationStep

LOG = logging.getLogger(__name__)
CONFIG_KEY = "TerraformVarsHypervisor"
APPLICATION = "openstack-hypervisor"
HYPERVISOR_APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
HYPERVISOR_UNIT_TIMEOUT = (
    1200  # 15 minutes, adding / removing units can take a long time
)


class DeployHypervisorApplicationStep(DeployMachineApplicationStep):
    """Deploy openstack-hyervisor application using Terraform cloud"""

    _CONFIG = CONFIG_KEY

    def __init__(
        self,
        client: Client,
        manifest: Manifest,
        jhelper: JujuHelper,
        model: str,
    ):
        super().__init__(
            client,
            manifest,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "hypervisor-plan",
            "Deploy OpenStack Hypervisor",
            "Deploying OpenStack Hypervisor",
        )
        self.openstack_model = OPENSTACK_MODEL

    def extra_tfvars(self) -> dict:
        tfhelper_openstack = self.manifest.get_tfhelper("openstack-plan")
        openstack_backend_config = tfhelper_openstack.backend_config()
        return {
            "openstack_model": self.openstack_model,
            "openstack-state-backend": tfhelper_openstack.backend,
            "openstack-state-config": openstack_backend_config,
        }

    def get_application_timeout(self) -> int:
        return HYPERVISOR_APP_TIMEOUT


class AddHypervisorUnitsStep(AddMachineUnitsStep):
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
            "Add Openstack-Hypervisor unit(s)",
            "Adding Openstack Hypervisor unit to machine(s)",
        )

    def get_unit_timeout(self) -> int:
        return HYPERVISOR_UNIT_TIMEOUT


class RemoveHypervisorUnitStep(BaseStep, JujuStepHelper):
    def __init__(
        self,
        client: Client,
        name: str,
        jhelper: JujuHelper,
        model: str,
        force: bool = False,
    ):
        super().__init__(
            "Remove openstack-hypervisor unit",
            "Remove openstack-hypervisor unit from machine",
        )
        self.client = client
        self.name = name
        self.jhelper = jhelper
        self.model = model
        self.force = force
        self.unit = None
        self.machine_id = ""

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        try:
            node = self.client.cluster.get_node_info(self.name)
            self.machine_id = str(node.get("machineid"))
        except NodeNotExistInClusterException:
            LOG.debug(f"Machine {self.name} does not exist, skipping.")
            return Result(ResultType.SKIPPED)

        try:
            application = run_sync(
                self.jhelper.get_application(APPLICATION, self.model)
            )
        except ApplicationNotFoundException as e:
            LOG.debug(str(e))
            return Result(
                ResultType.SKIPPED, "Hypervisor application has not been deployed yet"
            )

        for unit in application.units:
            if unit.machine.id == self.machine_id:
                LOG.debug(f"Unit {unit.name} is deployed on machine: {self.machine_id}")
                self.unit = unit.name
                return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def remove_machine_id_from_tfvar(self) -> None:
        """Remove machine if from terraform vars saved in cluster db."""
        try:
            tfvars = read_config(self.client, CONFIG_KEY)
        except ConfigItemNotFoundException:
            tfvars = {}

        machine_ids = tfvars.get("machine_ids", [])
        if self.machine_id in machine_ids:
            machine_ids.remove(self.machine_id)
            tfvars.update({"machine_ids": machine_ids})
            update_config(self.client, CONFIG_KEY, tfvars)

    def run(self, status: Optional[Status] = None) -> Result:
        """Remove unit from openstack-hypervisor application on Juju model."""
        try:
            self.guests = guests_on_hypervisor(self.name, self.jhelper)
            LOG.debug(f"Found guests on {self.name}:")
            LOG.debug(", ".join([g.name for g in self.guests]))
        except openstack.exceptions.SDKException as e:
            LOG.error("Encountered error looking up guests on hypervisor.")
            if self.force:
                LOG.warning("Force mode set, ignoring exception:")
                traceback.print_exception(e)
            else:
                return Result(ResultType.FAILED, str(e))
        if not self.force and len(self.guests) > 0:
            return Result(
                ResultType.FAILED,
                f"OpenStack guests are running on {self.name}, aborting",
            )
        try:
            run_sync(self.jhelper.remove_unit(APPLICATION, str(self.unit), self.model))
            self.remove_machine_id_from_tfvar()
            run_sync(
                self.jhelper.wait_application_ready(
                    APPLICATION,
                    self.model,
                    accepted_status=["active", "unknown"],
                    timeout=HYPERVISOR_UNIT_TIMEOUT,
                )
            )
        except (ApplicationNotFoundException, TimeoutException) as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))
        try:
            remove_hypervisor(self.name, self.jhelper)
        except openstack.exceptions.SDKException as e:
            LOG.error(
                "Encountered error removing hypervisor references from control plane."
            )
            if self.force:
                LOG.warning("Force mode set, ignoring exception:")
                traceback.print_exception(e)
            else:
                return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class ReapplyHypervisorTerraformPlanStep(BaseStep):
    """Reapply openstack-hyervisor terraform plan"""

    _CONFIG = CONFIG_KEY

    def __init__(
        self,
        client: Client,
        manifest: Manifest,
        jhelper: JujuHelper,
        model: str,
        extra_tfvars: dict = {},
    ):
        super().__init__(
            "Reapply OpenStack Hypervisor Terraform plan",
            "Reapply OpenStack Hypervisor Terraform plan",
        )
        self.client = client
        self.manifest = manifest
        self.jhelper = jhelper
        self.model = model
        self.extra_tfvars = extra_tfvars
        self.tfplan = "hypervisor-plan"

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        if self.client.cluster.list_nodes_by_role("compute"):
            return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Apply terraform configuration to deploy hypervisor"""
        try:
            self.manifest.update_tfvars_and_apply_tf(
                self.client,
                tfplan=self.tfplan,
                tfvar_config=self._CONFIG,
                override_tfvars=self.extra_tfvars,
            )
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))

        try:
            run_sync(
                self.jhelper.wait_application_ready(
                    APPLICATION,
                    self.model,
                    accepted_status=["active", "unknown"],
                    timeout=HYPERVISOR_APP_TIMEOUT,
                )
            )
        except TimeoutException as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)
