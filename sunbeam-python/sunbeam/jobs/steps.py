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
from typing import Optional

from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ConfigItemNotFoundException,
    NodeNotExistInClusterException,
)
from sunbeam.commands.terraform import TerraformException, TerraformHelper
from sunbeam.jobs.common import BaseStep, Result, ResultType, read_config, update_config
from sunbeam.jobs.juju import (
    ApplicationNotFoundException,
    JujuHelper,
    TimeoutException,
    run_sync,
)
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)


class DeployMachineApplicationStep(BaseStep):
    """Base class to deploy machine application using Terraform cloud"""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        config: str,
        application: str,
        model: str,
        banner: str = "",
        description: str = "",
        refresh: bool = False,
    ):
        super().__init__(banner, description)
        self.client = client
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = manifest
        self.config = config
        self.application = application
        self.model = model
        # Set refresh flag to True to redeploy the application
        self.refresh = refresh

    def extra_tfvars(self) -> dict:
        return {}

    def get_application_timeout(self) -> int:
        return 600

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        if self.refresh:
            return Result(ResultType.COMPLETED)

        try:
            run_sync(self.jhelper.get_application(self.application, self.model))
        except ApplicationNotFoundException:
            return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Apply terraform configuration to deploy sunbeam machine"""
        machine_ids = []
        try:
            app = run_sync(self.jhelper.get_application(self.application, self.model))
            machine_ids.extend(unit.machine.id for unit in app.units)
        except ApplicationNotFoundException as e:
            LOG.debug(str(e))

        try:
            extra_tfvars = self.extra_tfvars()
            extra_tfvars.update(
                {
                    "machine_ids": machine_ids,
                    "machine_model": self.model,
                }
            )
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self.config,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))

        # Note(gboutry): application is in state unknown when it's deployed
        # without units
        try:
            run_sync(
                self.jhelper.wait_application_ready(
                    self.application,
                    self.model,
                    accepted_status=["active", "unknown"],
                    timeout=self.get_application_timeout(),
                )
            )
        except TimeoutException as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class AddMachineUnitsStep(BaseStep):
    """Base class to add units of machine application"""

    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        config: str,
        application: str,
        model: str,
        banner: str = "",
        description: str = "",
    ):
        super().__init__(banner, description)
        self.client = client
        if isinstance(names, str):
            names = [names]
        self.names = names
        self.jhelper = jhelper
        self.config = config
        self.application = application
        self.model = model
        self.to_deploy = set()

    def get_unit_timeout(self) -> int:
        return 600  # 10 minutes

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        if len(self.names) == 0:
            return Result(ResultType.SKIPPED)
        nodes: list[dict] = self.client.cluster.list_nodes()

        filtered_nodes = list(filter(lambda node: node["name"] in self.names, nodes))
        if len(filtered_nodes) != len(self.names):
            filtered_node_names = [node["name"] for node in filtered_nodes]
            missing_nodes = set(self.names) - set(filtered_node_names)
            return Result(
                ResultType.FAILED,
                f"Nodes '{','.join(missing_nodes)}' do not exist in cluster database",
            )

        nodes_without_machine_id = []

        for node in filtered_nodes:
            node_machine_id = node.get("machineid", -1)
            if node_machine_id == -1:
                nodes_without_machine_id.append(node["name"])
                continue
            self.to_deploy.add(str(node_machine_id))

        if len(nodes_without_machine_id) > 0:
            return Result(
                ResultType.FAILED,
                f"Nodes '{','.join(nodes_without_machine_id)}' do not have machine id,"
                " are they deployed?",
            )
        try:
            app = run_sync(self.jhelper.get_application(self.application, self.model))
        except ApplicationNotFoundException:
            return Result(
                ResultType.FAILED,
                f"Application {self.application} has not been deployed",
            )

        deployed_units_machine_ids = set(unit.machine.id for unit in app.units)
        self.to_deploy -= deployed_units_machine_ids
        if len(self.to_deploy) == 0:
            return Result(ResultType.SKIPPED, "No new units to deploy")

        return Result(ResultType.COMPLETED)

    def add_machine_id_to_tfvar(self) -> None:
        """Add machine id to terraform vars saved in cluster db."""
        try:
            tfvars = read_config(self.client, self.config)
        except ConfigItemNotFoundException:
            tfvars = {}

        machine_ids = set(tfvars.get("machine_ids", []))

        if len(self.to_deploy) > 0 and self.to_deploy.issubset(machine_ids):
            LOG.debug("All machine ids are already in tfvars, skipping update")
            return

        machine_ids.update(self.to_deploy)
        tfvars.update({"machine_ids": sorted(machine_ids)})
        update_config(self.client, self.config, tfvars)

    def run(self, status: Optional[Status] = None) -> Result:
        """Add unit to machine application on Juju model."""
        try:
            units = run_sync(
                self.jhelper.add_unit(
                    self.application, self.model, sorted(self.to_deploy)
                )
            )
            self.add_machine_id_to_tfvar()
            run_sync(
                self.jhelper.wait_units_ready(
                    units,
                    self.model,
                    timeout=self.get_unit_timeout(),
                )
            )
        except (ApplicationNotFoundException, TimeoutException) as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveMachineUnitStep(BaseStep):
    """Base class to remove unit of machine application"""

    def __init__(
        self,
        client: Client,
        name: str,
        jhelper: JujuHelper,
        config: str,
        application: str,
        model: str,
        banner: str = "",
        description: str = "",
    ):
        super().__init__(banner, description)
        self.client = client
        self.name = name
        self.jhelper = jhelper
        self.config = config
        self.application = application
        self.model = model
        self.machine_id = ""
        self.unit = None

    def get_unit_timeout(self) -> int:
        return 600  # 10 minutes

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
            app = run_sync(self.jhelper.get_application(self.application, self.model))
        except ApplicationNotFoundException as e:
            LOG.debug(str(e))
            return Result(
                ResultType.SKIPPED,
                "Application {self.application} has not been deployed yet",
            )

        for unit in app.units:
            if unit.machine.id == self.machine_id:
                LOG.debug(f"Unit {unit.name} is deployed on machine: {self.machine_id}")
                self.unit = unit.name
                return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Remove unit from machine application on Juju model."""
        try:
            run_sync(
                self.jhelper.remove_unit(self.application, str(self.unit), self.model)
            )
            run_sync(
                self.jhelper.wait_application_ready(
                    self.application,
                    self.model,
                    accepted_status=["active", "unknown"],
                    timeout=self.get_unit_timeout(),
                )
            )
        except (ApplicationNotFoundException, TimeoutException) as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)
