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

import ast
import logging
from typing import Optional

import click
from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.jobs import questions
from sunbeam.jobs.common import BaseStep, Result, ResultType
from sunbeam.jobs.juju import (
    ActionFailedException,
    ApplicationNotFoundException,
    JujuHelper,
    LeaderNotFoundException,
    UnitNotFoundException,
    run_sync,
)
from sunbeam.jobs.manifest import Manifest
from sunbeam.jobs.steps import (
    AddMachineUnitsStep,
    DeployMachineApplicationStep,
    RemoveMachineUnitStep,
)

LOG = logging.getLogger(__name__)
CONFIG_KEY = "TerraformVarsMicrocephPlan"
CONFIG_DISKS_KEY = "TerraformVarsMicroceph"
APPLICATION = "microceph"
MICROCEPH_APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
MICROCEPH_UNIT_TIMEOUT = (
    1200  # 15 minutes, adding / removing units can take a long time
)


def microceph_questions():
    return {
        "osd_devices": questions.PromptQuestion(
            "Disks to attach to MicroCeph (comma separated list)",
        ),
    }


async def list_disks(jhelper: JujuHelper, model: str, unit: str) -> tuple[dict, dict]:
    """Call list-disks action on an unit."""
    LOG.debug("Running list-disks on : %r", unit)
    action_result = await jhelper.run_action(unit, model, "list-disks")
    LOG.debug(
        "Result after running action list-disks on %r: %r",
        unit,
        action_result,
    )
    osds = ast.literal_eval(action_result.get("osds", "[]"))
    unpartitioned_disks = ast.literal_eval(
        action_result.get("unpartitioned-disks", "[]")
    )
    return osds, unpartitioned_disks


def ceph_replica_scale(storage_nodes: int) -> int:
    return min(storage_nodes, 3)


class DeployMicrocephApplicationStep(DeployMachineApplicationStep):
    """Deploy Microceph application using Terraform"""

    def __init__(
        self,
        client: Client,
        manifest: Manifest,
        jhelper: JujuHelper,
        model: str,
        refresh: bool = False,
    ):
        super().__init__(
            client,
            manifest,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "microceph-plan",
            "Deploy MicroCeph",
            "Deploying MicroCeph",
            refresh,
        )

    def get_application_timeout(self) -> int:
        return MICROCEPH_APP_TIMEOUT

    def extra_tfvars(self) -> dict:
        storage_nodes = self.client.cluster.list_nodes_by_role("storage")
        if len(storage_nodes):
            return {
                "charm_microceph_config": {
                    "default-pool-size": ceph_replica_scale(len(storage_nodes))
                }
            }

        return {}


class AddMicrocephUnitsStep(AddMachineUnitsStep):
    """Add Microceph Unit."""

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
            "Add MicroCeph unit",
            "Adding MicroCeph unit to machine",
        )

    def get_unit_timeout(self) -> int:
        return MICROCEPH_UNIT_TIMEOUT


class RemoveMicrocephUnitStep(RemoveMachineUnitStep):
    """Remove Microceph Unit."""

    def __init__(self, client: Client, name: str, jhelper: JujuHelper, model: str):
        super().__init__(
            client,
            name,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Remove MicroCeph unit",
            "Removing MicroCeph unit from machine",
        )

    def get_unit_timeout(self) -> int:
        return MICROCEPH_UNIT_TIMEOUT


class ConfigureMicrocephOSDStep(BaseStep):
    """Configure Microceph OSD disks"""

    _CONFIG = CONFIG_DISKS_KEY

    def __init__(
        self,
        client: Client,
        name: str,
        jhelper: JujuHelper,
        model: str,
        deployment_preseed: dict | None = None,
        accept_defaults: bool = False,
    ):
        super().__init__("Configure MicroCeph storage", "Configuring MicroCeph storage")
        self.client = client
        self.node_name = name
        self.jhelper = jhelper
        self.model = model
        self.preseed = deployment_preseed
        self.accept_defaults = accept_defaults
        self.variables = {}
        self.machine_id = ""
        self.disks = ""
        self.unpartitioned_disks = []
        self.osd_disks = []

    def microceph_config_questions(self):
        disks_str = None
        if len(self.unpartitioned_disks) > 0:
            disks_str = ",".join(self.unpartitioned_disks)

        questions = microceph_questions()
        # Specialise question with local disk information.
        questions["osd_devices"].default_value = disks_str
        return questions

    def get_all_disks(self) -> None:
        try:
            node = self.client.cluster.get_node_info(self.node_name)
            self.machine_id = str(node.get("machineid"))
            unit = run_sync(
                self.jhelper.get_unit_from_machine(
                    APPLICATION, self.machine_id, self.model
                )
            )
            self.osd_disks, self.unpartitioned_disks = run_sync(
                list_disks(self.jhelper, self.model, unit.entity_id)
            )
            self.unpartitioned_disks = [
                disk.get("path") for disk in self.unpartitioned_disks
            ]
            self.osd_disks = [disk.get("path") for disk in self.osd_disks]
            LOG.debug(f"Unpartitioned disks: {self.unpartitioned_disks}")
            LOG.debug(f"OSD disks: {self.osd_disks}")

        except (UnitNotFoundException, ActionFailedException) as e:
            LOG.debug(str(e))
            raise click.ClickException("Unable to list disks")

    def prompt(self, console: Optional[Console] = None) -> None:
        """Determines if the step can take input from the user.

        Prompts are used by Steps to gather the necessary input prior to
        running the step. Steps should not expect that the prompt will be
        available and should provide a reasonable default where possible.
        """
        self.get_all_disks()
        self.variables = questions.load_answers(self.client, self._CONFIG)
        self.variables.setdefault("microceph_config", {})
        self.variables["microceph_config"].setdefault(
            self.node_name, {"osd_devices": None}
        )

        # Set defaults
        self.preseed.setdefault("microceph_config", {})
        self.preseed["microceph_config"].setdefault(
            self.node_name, {"osd_devices": None}
        )

        # Preseed can have osd_devices as list. If so, change to comma separated str
        osd_devices = (
            self.preseed.get("microceph_config", {})
            .get(self.node_name, {})
            .get("osd_devices")
        )
        if isinstance(osd_devices, list):
            osd_devices_str = ",".join(osd_devices)
            self.preseed["microceph_config"][self.node_name][
                "osd_devices"
            ] = osd_devices_str

        microceph_config_bank = questions.QuestionBank(
            questions=self.microceph_config_questions(),
            console=console,  # type: ignore
            preseed=self.preseed.get("microceph_config", {}).get(self.node_name),
            previous_answers=self.variables.get("microceph_config", {}).get(
                self.node_name
            ),
            accept_defaults=self.accept_defaults,
        )
        # Microceph configuration
        self.disks = microceph_config_bank.osd_devices.ask()
        self.variables["microceph_config"][self.node_name]["osd_devices"] = self.disks

        LOG.debug(self.variables)
        questions.write_answers(self.client, self._CONFIG, self.variables)

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user.

        :return: True if the step can ask the user for prompts,
                 False otherwise
        """
        return True

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        if not self.disks:
            LOG.debug(
                "Skipping ConfigureMicrocephOSDStep as no osd devices are selected"
            )
            return Result(ResultType.SKIPPED)

        # Remove any disks that are already added
        disks_to_add = self.disks.split(",")
        disks_to_add = set(disks_to_add).difference(set(self.osd_disks))
        self.disks = ",".join(disks_to_add)
        if not self.disks:
            LOG.debug("Skipping ConfigureMicrocephOSDStep as devices are already added")
            return Result(ResultType.SKIPPED)

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Configure local disks on microceph."""
        try:
            unit = run_sync(
                self.jhelper.get_unit_from_machine(
                    APPLICATION, self.machine_id, self.model
                )
            )
            LOG.debug(f"Running action add-osd on {unit.entity_id}")
            action_result = run_sync(
                self.jhelper.run_action(
                    unit.entity_id,
                    self.model,
                    "add-osd",
                    action_params={
                        "device-id": self.disks,
                    },
                )
            )
            LOG.debug(f"Result after running action add-osd: {action_result}")
        except (UnitNotFoundException, ActionFailedException) as e:
            message = f"Microceph Adding disks {self.disks} failed: {str(e)}"
            LOG.debug(message)
            return Result(ResultType.FAILED, message)

        return Result(ResultType.COMPLETED)


class SetCephMgrPoolSizeStep(BaseStep):
    """Configure Microceph pool size for mgr"""

    def __init__(self, client: Client, jhelper: JujuHelper, model: str):
        super().__init__(
            "Set Microceph mgr Pool size",
            "Setting Microceph mgr pool size",
        )
        self.client = client
        self.jhelper = jhelper
        self.model = model
        self.storage_nodes = []

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.storage_nodes = self.client.cluster.list_nodes_by_role("storage")
        if len(self.storage_nodes):
            return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Set ceph mgr pool size."""
        try:
            unit = run_sync(self.jhelper.get_leader_unit(APPLICATION, self.model))
            action_params = {
                "pools": ".mgr",
                "size": ceph_replica_scale(len(self.storage_nodes)),
            }
            LOG.debug(
                f"Running microceph action set-pool-size with params {action_params}"
            )
            result = run_sync(
                self.jhelper.run_action(
                    unit, self.model, "set-pool-size", action_params
                )
            )
            if result.get("status") is None:
                return Result(
                    ResultType.FAILED,
                    "ERROR: Failed to update pool size for .mgr",
                )
        except (
            ApplicationNotFoundException,
            LeaderNotFoundException,
            ActionFailedException,
        ) as e:
            LOG.debug("Failed to update pool size for .mgr", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)
