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

from rich.console import Console
from rich.status import Status

from sunbeam.commands.hypervisor import ReapplyHypervisorTerraformPlanStep
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.k8s import DeployK8SApplicationStep
from sunbeam.commands.microceph import DeployMicrocephApplicationStep
from sunbeam.commands.microk8s import DeployMicrok8sApplicationStep
from sunbeam.commands.openstack import ReapplyOpenStackTerraformPlanStep
from sunbeam.commands.sunbeam_machine import DeploySunbeamMachineApplicationStep
from sunbeam.commands.terraform import TerraformInitStep
from sunbeam.commands.upgrades.base import UpgradeCoordinator, UpgradePlugins
from sunbeam.jobs.common import BaseStep, Result, ResultType
from sunbeam.jobs.juju import JujuHelper, run_sync
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)
console = Console()


class LatestInChannel(BaseStep, JujuStepHelper):
    def __init__(self, jhelper: JujuHelper, manifest: Manifest):
        """Upgrade all charms to latest in current channel.

        :jhelper: Helper for interacting with pylibjuju
        """
        super().__init__(
            "In channel upgrade", "Upgrade charms to latest revision in current channel"
        )
        self.jhelper = jhelper
        self.manifest = manifest

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Step can be skipped if nothing needs refreshing."""
        return Result(ResultType.COMPLETED)

    def is_track_changed_for_any_charm(self, deployed_apps: dict):
        """Check if chanel track is same in manifest and deployed app."""
        for app_name, (charm, channel, _) in deployed_apps.items():
            if not self.manifest.software.charms.get(charm):
                LOG.debug(f"Charm not present in manifest: {charm}")
                continue

            channel_from_manifest = (
                self.manifest.software.charms.get(charm).channel or ""
            )
            track_from_manifest = channel_from_manifest.split("/")[0]
            track_from_deployed_app = channel.split("/")[0]
            # Compare tracks
            if track_from_manifest != track_from_deployed_app:
                LOG.debug(
                    f"Channel track for app {app_name} different in manifest "
                    "and actual deployed"
                )
                return True

        return False

    def refresh_apps(self, apps: dict, model: str) -> None:
        """Refresh apps in the model.

        If the charm has no revision in manifest and channel mentioned in manifest
        and the deployed app is same, run juju refresh.
        Otherwise ignore so that terraform plan apply will take care of charm upgrade.
        """
        for app_name, (charm, channel, _) in apps.items():
            manifest_charm = self.manifest.software.charms.get(charm)
            if not manifest_charm:
                continue

            if not manifest_charm.revision and manifest_charm.channel == channel:
                app = run_sync(self.jhelper.get_application(app_name, model))
                LOG.debug(f"Running refresh for app {app_name}")
                # refresh() checks for any new revision and updates if available
                run_sync(app.refresh())

    def run(self, status: Optional[Status] = None) -> Result:
        """Refresh all charms identified as needing a refresh.

        If the manifest has charm channel and revision, terraform apply should update
        the charms.
        If the manifest has only charm, then juju refresh is required if channel is
        same as deployed charm, otherwise juju upgrade charm.
        """
        deployed_k8s_apps = self.get_charm_deployed_versions("openstack")
        deployed_machine_apps = self.get_charm_deployed_versions("controller")

        all_deployed_apps = deployed_k8s_apps.copy()
        all_deployed_apps.update(deployed_machine_apps)
        LOG.debug(f"All deployed apps: {all_deployed_apps}")
        if self.is_track_changed_for_any_charm(all_deployed_apps):
            error_msg = (
                "Manifest has track values that require upgrades, rerun with "
                "option --upgrade-release for release upgrades."
            )
            return Result(ResultType.FAILED, error_msg)

        self.refresh_apps(deployed_k8s_apps, "openstack")
        self.refresh_apps(deployed_machine_apps, "controller")
        return Result(ResultType.COMPLETED)


class LatestInChannelCoordinator(UpgradeCoordinator):
    """Coordinator for refreshing charms in their current channel."""

    def get_plan(self) -> list[BaseStep]:
        plan = [
            LatestInChannel(self.jhelper, self.manifest),
            TerraformInitStep(self.deployment.get_tfhelper("openstack-plan")),
            ReapplyOpenStackTerraformPlanStep(
                self.client,
                self.deployment.get_tfhelper("openstack-plan"),
                self.jhelper,
                self.manifest,
            ),
            TerraformInitStep(self.deployment.get_tfhelper("sunbeam-machine-plan")),
            DeploySunbeamMachineApplicationStep(
                self.client,
                self.deployment.get_tfhelper("sunbeam-machine-plan"),
                self.jhelper,
                self.manifest,
                self.deployment.infrastructure_model,
                refresh=True,
            ),
        ]

        if self.k8s_provider == "k8s":
            plan.extend(
                [
                    TerraformInitStep(self.deployment.get_tfhelper("k8s-plan")),
                    DeployK8SApplicationStep(
                        self.client,
                        self.deployment.get_tfhelper("k8s-plan"),
                        self.jhelper,
                        self.manifest,
                        self.deployment.infrastructure_model,
                        refresh=True,
                    ),
                ]
            )
        else:
            plan.extend(
                [
                    TerraformInitStep(self.deployment.get_tfhelper("microk8s-plan")),
                    DeployMicrok8sApplicationStep(
                        self.client,
                        self.deployment.get_tfhelper("microk8s-plan"),
                        self.jhelper,
                        self.manifest,
                        self.deployment.infrastructure_model,
                        refresh=True,
                    ),
                ]
            )

        plan.extend(
            [
                TerraformInitStep(self.deployment.get_tfhelper("microceph-plan")),
                DeployMicrocephApplicationStep(
                    self.client,
                    self.deployment.get_tfhelper("microceph-plan"),
                    self.jhelper,
                    self.manifest,
                    self.deployment.infrastructure_model,
                    refresh=True,
                ),
                TerraformInitStep(self.deployment.get_tfhelper("hypervisor-plan")),
                ReapplyHypervisorTerraformPlanStep(
                    self.client,
                    self.deployment.get_tfhelper("hypervisor-plan"),
                    self.jhelper,
                    self.manifest,
                    self.deployment.infrastructure_model,
                ),
                UpgradePlugins(self.deployment, upgrade_release=False),
            ]
        )

        return plan
