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
from typing import List, Optional

from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.client import Client
from sunbeam.commands.hypervisor import CONFIG_KEY as HYPERVISOR_CONFIG_KEY
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.k8s import K8S_CONFIG_KEY
from sunbeam.commands.microceph import CONFIG_KEY as MICROCEPH_CONFIG_KEY
from sunbeam.commands.microk8s import MICROK8S_CONFIG_KEY
from sunbeam.commands.openstack import CONFIG_KEY as OPENSTACK_CONFIG_KEY
from sunbeam.commands.openstack import OPENSTACK_DEPLOY_TIMEOUT
from sunbeam.commands.sunbeam_machine import CONFIG_KEY as SUNBEAM_MACHINE_CONFIG_KEY
from sunbeam.commands.terraform import TerraformException, TerraformHelper
from sunbeam.commands.upgrades.base import UpgradeCoordinator, UpgradePlugins
from sunbeam.jobs.common import (
    BaseStep,
    Result,
    ResultType,
    run_plan,
    update_status_background,
)
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper, JujuWaitException, TimeoutException, run_sync
from sunbeam.jobs.manifest import Manifest
from sunbeam.jobs.plugin import PluginManager
from sunbeam.versions import (
    MISC_CHARMS_K8S,
    MYSQL_CHARMS_K8S,
    OPENSTACK_CHARMS_K8S,
    OVN_CHARMS_K8S,
)

LOG = logging.getLogger(__name__)
console = Console()


class BaseUpgrade(BaseStep, JujuStepHelper):
    def __init__(
        self,
        name: str,
        description: str,
        client: Client,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of BaseUpgrade class.

        :client: Client for interacting with clusterd
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(name, description)
        self.client = client
        self.jhelper = jhelper
        self.manifest = manifest
        self.model = model

    def run(self, status: Optional[Status] = None) -> Result:
        """Run control plane and machine charm upgrade."""
        result = self.pre_upgrade_tasks(status)
        if result.result_type == ResultType.FAILED:
            return result

        self.upgrade_tasks(status)
        if result.result_type == ResultType.FAILED:
            return result

        result = self.post_upgrade_tasks(status)
        return result

    def pre_upgrade_tasks(self, status: Optional[Status] = None) -> Result:
        """Tasks to run before the upgrade."""
        return Result(ResultType.COMPLETED)

    def post_upgrade_tasks(self, status: Optional[Status] = None) -> Result:
        """Tasks to run after the upgrade."""
        return Result(ResultType.COMPLETED)

    def upgrade_applications(
        self,
        apps: List[str],
        charms: List[str],
        model: str,
        tfhelper: TerraformHelper,
        config: str,
        timeout: int,
        status: Optional[Status] = None,
    ) -> Result:
        """Upgrade applications.

        :param apps: List of applications to be upgraded
        :param charms: List of charms
        :param model: Name of model
        :param tfhelper: Tfhelper of associated plan
        :param config: Terraform config key used to store config in clusterdb
        :param timeout: Timeout to wait for apps in expected status
        :param status: Status object to update charm status
        """
        expected_wls = ["active", "blocked", "unknown"]
        LOG.debug(
            f"Upgrading applications using terraform plan {tfhelper.plan}: {apps}"
        )
        try:
            tfhelper.update_partial_tfvars_and_apply_tf(
                self.client, self.manifest, charms, config
            )
        except TerraformException as e:
            LOG.exception("Error upgrading cloud")
            return Result(ResultType.FAILED, str(e))

        task = run_sync(update_status_background(self, apps, status))
        try:
            run_sync(
                self.jhelper.wait_until_desired_status(
                    model,
                    apps,
                    expected_wls,
                    timeout=timeout,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug(str(e))
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class UpgradeControlPlane(BaseUpgrade):
    def __init__(
        self,
        deployment: Deployment,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of BaseUpgrade class.

        :client: Client for interacting with clusterd
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade Openstack charms",
            "Upgrading Openstack charms",
            client,
            jhelper,
            manifest,
            model,
        )
        self.deployment = deployment
        self.tfhelper = tfhelper
        self.config = OPENSTACK_CONFIG_KEY

    def upgrade_tasks(self, status: Optional[Status] = None) -> Result:
        # Step 1: Upgrade mysql charms
        LOG.debug("Upgrading Mysql charms")
        charms = list(MYSQL_CHARMS_K8S.keys())
        apps = self.get_apps_filter_by_charms(self.model, charms)
        result = self.upgrade_applications(
            apps, charms, self.model, self.tfhelper, self.config, 1200, status
        )
        if result.result_type == ResultType.FAILED:
            return result

        # Step 2: Upgrade all openstack core charms
        LOG.debug("Upgrading openstack core charms")
        charms = (
            list(MISC_CHARMS_K8S.keys())
            + list(OVN_CHARMS_K8S.keys())  # noqa: W503
            + list(OPENSTACK_CHARMS_K8S.keys())  # noqa: W503
        )
        apps = self.get_apps_filter_by_charms(self.model, charms)
        result = self.upgrade_applications(
            apps,
            charms,
            self.model,
            self.tfhelper,
            self.config,
            OPENSTACK_DEPLOY_TIMEOUT,
            status,
        )
        if result.result_type == ResultType.FAILED:
            return result

        # Step 3: Upgrade all plugins that uses openstack-plan
        LOG.debug("Upgrading openstack plugins that are enabled")
        # TODO(gboutry): We have a loaded manifest, can't we get charms from there ?
        charms = PluginManager().get_all_charms_in_openstack_plan(self.deployment)
        apps = self.get_apps_filter_by_charms(self.model, charms)
        result = self.upgrade_applications(
            apps,
            charms,
            self.model,
            self.tfhelper,
            self.config,
            OPENSTACK_DEPLOY_TIMEOUT,
            status,
        )
        return result


class UpgradeMachineCharm(BaseUpgrade):
    def __init__(
        self,
        name: str,
        description: str,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
        charms: list,
        config: str,
        timeout: int,
    ):
        """Create instance of BaseUpgrade class.

        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        :charms: List of charms to upgrade
        :tfplan: Terraform plan to reapply
        :config: Config key used to save tfvars in clusterdb
        :timeout: Time to wait for apps to come to desired status
        """
        super().__init__(
            name,
            description,
            client,
            jhelper,
            manifest,
            model,
        )
        self.charms = charms
        self.tfhelper = tfhelper
        self.config = config
        self.timeout = timeout

    def upgrade_tasks(self, status: Optional[Status] = None) -> Result:
        apps = self.get_apps_filter_by_charms(self.model, self.charms)
        result = self.upgrade_applications(
            apps,
            self.charms,
            self.model,
            self.tfhelper,
            self.config,
            self.timeout,
            status,
        )

        return result


class UpgradeMicrocephCharm(UpgradeMachineCharm):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of UpgradeMicrocephCharm class.

        :client: Client to connect to clusterdb
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade Microceph charm",
            "Upgrading microceph charm",
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            ["microceph"],
            MICROCEPH_CONFIG_KEY,
            1200,
        )


class UpgradeMicrok8sCharm(UpgradeMachineCharm):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of UpgradeMicrok8sCharm class.

        :client: Client to connect to clusterdb
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade Microk8s charm",
            "Upgrading Microk8s charm",
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            ["microk8s"],
            MICROK8S_CONFIG_KEY,
            1200,
        )


class UpgradeK8SCharm(UpgradeMachineCharm):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of UpgradeK8SCharm class.

        :client: Client to connect to clusterdb
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade K8S charm",
            "Upgrading K8S charm",
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            ["k8s"],
            K8S_CONFIG_KEY,
            1200,
        )


class UpgradeOpenstackHypervisorCharm(UpgradeMachineCharm):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of UpgradeOpenstackHypervisorCharm class.

        :client: Client to connect to clusterdb
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade hypervisor charm",
            "Upgrading hypervisor charm",
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            ["openstack-hypervisor"],
            HYPERVISOR_CONFIG_KEY,
            1200,
        )


class UpgradeSunbeamMachineCharm(UpgradeMachineCharm):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        """Create instance of UpgradeSunbeamMachineCharm class.

        :client: Client to connect to clusterdb
        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        :model: Name of model containing charms.
        """
        super().__init__(
            "Upgrade sunbeam-machine charm",
            "Upgrading sunbeam-machine charm",
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            ["sunbeam-machine"],
            SUNBEAM_MACHINE_CONFIG_KEY,
            1200,
        )


class ChannelUpgradeCoordinator(UpgradeCoordinator):
    def get_plan(self) -> list[BaseStep]:
        """Return the plan for this upgrade.

        Return the steps to complete this upgrade.
        """
        get_tf = self.deployment.get_tfhelper
        plan = [
            ValidationCheck(self.jhelper, self.manifest),
            UpgradeControlPlane(
                self.deployment,
                self.client,
                get_tf("openstack-plan"),
                self.jhelper,
                self.manifest,
                "openstack",
            ),
            UpgradeMicrocephCharm(
                self.client,
                get_tf("microceph-plan"),
                self.jhelper,
                self.manifest,
                "controller",
            ),
        ]
        if self.k8s_provider == "k8s":
            plan.append(
                UpgradeK8SCharm(
                    self.client,
                    get_tf("k8s-plan"),
                    self.jhelper,
                    self.manifest,
                    "controller",
                )
            )
        else:
            plan.append(
                UpgradeMicrok8sCharm(
                    self.client,
                    get_tf("microk8s-plan"),
                    self.jhelper,
                    self.manifest,
                    "controller",
                )
            )

        plan.extend(
            [
                UpgradeOpenstackHypervisorCharm(
                    self.client,
                    get_tf("openstack-hypervisor-plan"),
                    self.jhelper,
                    self.manifest,
                    "controller",
                ),
                UpgradeSunbeamMachineCharm(
                    self.client,
                    get_tf("sunbeam-machine-plan"),
                    self.jhelper,
                    self.manifest,
                    "controller",
                ),
                UpgradePlugins(self.deployment, upgrade_release=True),
            ]
        )
        return plan

    def run_plan(self) -> None:
        """Execute the upgrade plan."""
        plan = self.get_plan()
        run_plan(plan, console)


class ValidationCheck(BaseStep):
    def __init__(self, jhelper: JujuHelper, manifest: Manifest):
        """Run validation on the deployment.

        Check whether the requested upgrade is possible.

        :jhelper: Helper for interacting with pylibjuju
        :manifest: Manifest object
        """
        super().__init__("Validation", "Running pre-upgrade validation")
        self.jhelper = jhelper
        self.manifest = manifest

    def run(self, status: Optional[Status] = None) -> Result:
        """Run validation check."""
        rabbit_channel = run_sync(
            self.jhelper.get_charm_channel("rabbitmq", "openstack")
        )
        if rabbit_channel.split("/")[0] == "3.9":
            return Result(
                ResultType.FAILED,
                (
                    "Pre-upgrade validation failed: Rabbit charm cannot be "
                    "upgraded from 3.9"
                ),
            )
        else:
            return Result(ResultType.COMPLETED)
