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

from lightkube.core import exceptions
from lightkube.core.client import Client as KubeClient
from lightkube.core.client import KubeConfig
from lightkube.resources.core_v1 import Service
from rich.status import Status

import sunbeam.commands.microceph as microceph
from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.k8s import CREDENTIAL_SUFFIX, K8SHelper
from sunbeam.commands.terraform import TerraformException, TerraformHelper
from sunbeam.jobs.common import (
    RAM_32_GB_IN_KB,
    BaseStep,
    Result,
    ResultType,
    convert_proxy_to_model_configs,
    get_host_total_ram,
    read_config,
    update_config,
    update_status_background,
)
from sunbeam.jobs.juju import JujuHelper, JujuWaitException, TimeoutException, run_sync
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)
OPENSTACK_MODEL = "openstack"
OPENSTACK_DEPLOY_TIMEOUT = 5400  # 90 minutes

CONFIG_KEY = "TerraformVarsOpenstack"
TOPOLOGY_KEY = "Topology"


def determine_target_topology(client: Client) -> str:
    """Determines the target topology.

    Use information from clusterdb to infer deployment
    topology.
    """
    control_nodes = client.cluster.list_nodes_by_role("control")
    compute_nodes = client.cluster.list_nodes_by_role("compute")
    combined = set(node["name"] for node in control_nodes + compute_nodes)
    host_total_ram = get_host_total_ram()
    if len(combined) == 1 and host_total_ram < RAM_32_GB_IN_KB:
        topology = "single"
    elif len(combined) < 10:
        topology = "multi"
    else:
        topology = "large"
    LOG.debug(f"Auto-detected topology: {topology}")
    return topology


def compute_ha_scale(topology: str, control_nodes: int) -> int:
    if topology == "single" or control_nodes < 3:
        return 1
    return 3


def compute_os_api_scale(topology: str, control_nodes: int) -> int:
    if topology == "single":
        return 1
    if topology == "multi" or control_nodes < 3:
        return min(control_nodes, 3)
    if topology == "large":
        return min(control_nodes + 2, 7)
    raise ValueError(f"Unknown topology {topology}")


def compute_ingress_scale(topology: str, control_nodes: int) -> int:
    if topology == "single":
        return 1
    return min(control_nodes, 3)


def compute_ceph_replica_scale(osds: int) -> int:
    return min(osds, 3)


async def _get_number_of_osds(jhelper: JujuHelper, model: str) -> int:
    """Fetch the number of osds from the microceph application"""
    leader = await jhelper.get_leader_unit(microceph.APPLICATION, model)
    osds, _ = await microceph.list_disks(jhelper, model, leader)
    return len(osds)


class DeployControlPlaneStep(BaseStep, JujuStepHelper):
    """Deploy OpenStack using Terraform cloud"""

    _CONFIG = CONFIG_KEY

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        topology: str,
        database: str,
        machine_model: str,
        proxy_settings: dict | None = None,
        force: bool = False,
    ):
        super().__init__(
            "Deploying OpenStack Control Plane",
            "Deploying OpenStack Control Plane to Kubernetes (this may take a while)",
        )
        self.client = client
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = manifest
        self.topology = topology
        self.database = database
        self.machine_model = machine_model
        self.proxy_settings = proxy_settings or {}
        self.force = force
        self.model = OPENSTACK_MODEL
        self.cloud = K8SHelper.get_cloud()

    def get_storage_tfvars(self, storage_nodes: list[dict]) -> dict:
        """Create terraform variables related to storage."""
        tfvars = {}
        if storage_nodes:
            tfvars["enable-ceph"] = True
            tfvars["ceph-offer-url"] = (
                f"admin/{self.machine_model}.{microceph.APPLICATION}"
            )
            tfvars["ceph-osd-replication-count"] = compute_ceph_replica_scale(
                run_sync(_get_number_of_osds(self.jhelper, self.machine_model))
            )
        else:
            tfvars["enable-ceph"] = False

        return tfvars

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.update_status(status, "determining appropriate configuration")
        try:
            previous_config = read_config(self.client, TOPOLOGY_KEY)
        except ConfigItemNotFoundException:
            # Config was never registered in database
            previous_config = {}

        determined_topology = determine_target_topology(self.client)

        if self.topology == "auto":
            self.topology = determined_topology
        LOG.debug(f"topology {self.topology}")

        if self.database == "auto":
            self.database = previous_config.get("database", determined_topology)
        if self.database == "large":
            # multi and large are the same
            self.database = "multi"
        LOG.debug(f"database topology {self.database}")
        if (database := previous_config.get("database")) and database != self.database:
            return Result(
                ResultType.FAILED,
                "Database topology cannot be changed, please destroy and re-bootstrap",
            )

        is_not_compatible = self.database == "single" and self.topology == "large"
        if not self.force and is_not_compatible:
            return Result(
                ResultType.FAILED,
                (
                    "Cannot deploy control plane to large with single database,"
                    " use -f/--force to override"
                ),
            )

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        # TODO(jamespage):
        # This needs to evolve to add support for things like:
        # - Enabling/disabling specific services
        update_config(
            self.client,
            TOPOLOGY_KEY,
            {"topology": self.topology, "database": self.database},
        )

        self.update_status(status, "fetching cluster nodes")
        control_nodes = self.client.cluster.list_nodes_by_role("control")
        storage_nodes = self.client.cluster.list_nodes_by_role("storage")

        self.update_status(status, "computing deployment sizing")
        model_config = convert_proxy_to_model_configs(self.proxy_settings)
        model_config.update({"workload-storage": K8SHelper.get_default_storageclass()})
        extra_tfvars = self.get_storage_tfvars(storage_nodes)
        extra_tfvars.update(
            {
                "model": self.model,
                "cloud": self.cloud,
                "credential": f"{self.cloud}{CREDENTIAL_SUFFIX}",
                "config": model_config,
                "many-mysql": self.database == "multi",
                "ha-scale": compute_ha_scale(self.topology, len(control_nodes)),
                "os-api-scale": compute_os_api_scale(self.topology, len(control_nodes)),
                "ingress-scale": compute_ingress_scale(
                    self.topology, len(control_nodes)
                ),
            }
        )
        self.update_status(status, "deploying services")
        try:
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error configuring cloud")
            return Result(ResultType.FAILED, str(e))

        # Remove cinder-ceph from apps to wait on if ceph is not enabled
        apps = run_sync(self.jhelper.get_application_names(self.model))
        if not extra_tfvars.get("enable-ceph") and "cinder-ceph" in apps:
            apps.remove("cinder-ceph")
        LOG.debug(f"Application monitored for readiness: {apps}")
        task = run_sync(update_status_background(self, apps, status))
        try:
            run_sync(
                self.jhelper.wait_until_active(
                    self.model,
                    apps,
                    timeout=OPENSTACK_DEPLOY_TIMEOUT,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class PatchLoadBalancerServicesStep(BaseStep):
    SERVICES = ["traefik", "traefik-public", "rabbitmq", "ovn-relay"]
    MODEL = OPENSTACK_MODEL

    def __init__(
        self,
        client: Client,
    ):
        super().__init__(
            "Patch LoadBalancer services",
            "Patch LoadBalancer service annotations",
        )
        self.client = client
        self.lb_annotation = K8SHelper.get_loadbalancer_annotation()

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        try:
            self.kubeconfig = read_config(self.client, K8SHelper.get_kubeconfig_key())
        except ConfigItemNotFoundException:
            LOG.debug("K8S kubeconfig not found", exc_info=True)
            return Result(ResultType.FAILED, "K8S kubeconfig not found")

        kubeconfig = KubeConfig.from_dict(self.kubeconfig)
        try:
            self.kube = KubeClient(kubeconfig, self.MODEL, trust_env=False)
        except exceptions.ConfigError as e:
            LOG.debug("Error creating k8s client", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        for service_name in self.SERVICES:
            service = self.kube.get(Service, service_name)
            service_annotations = service.metadata.annotations
            if self.lb_annotation not in service_annotations:
                return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Patch LoadBalancer services annotations with LB IP."""
        for service_name in self.SERVICES:
            service = self.kube.get(Service, service_name)
            service_annotations = service.metadata.annotations
            if self.lb_annotation not in service_annotations:
                loadbalancer_ip = service.status.loadBalancer.ingress[0].ip
                service_annotations[self.lb_annotation] = loadbalancer_ip
                LOG.debug(f"Patching {service_name!r} to use IP {loadbalancer_ip!r}")
                self.kube.patch(Service, service_name, obj=service)

        return Result(ResultType.COMPLETED)


class ReapplyOpenStackTerraformPlanStep(BaseStep, JujuStepHelper):
    """Reapply OpenStack Terraform plan"""

    _CONFIG = CONFIG_KEY

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
    ):
        super().__init__(
            "Applying Control plane Terraform plan",
            "Applying Control plane Terraform plan (this may take a while)",
        )
        self.client = client
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = manifest
        self.model = OPENSTACK_MODEL

    def run(self, status: Optional[Status] = None) -> Result:
        """Reapply Terraform plan if there are changes in tfvars."""
        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
            )
        except TerraformException as e:
            LOG.exception("Error reconfiguring cloud")
            return Result(ResultType.FAILED, str(e))

        storage_nodes = self.client.cluster.list_nodes_by_role("storage")
        # Remove cinder-ceph from apps to wait on if ceph is not enabled
        apps = run_sync(self.jhelper.get_application_names(self.model))
        if not storage_nodes and "cinder-ceph" in apps:
            apps.remove("cinder-ceph")
        LOG.debug(f"Application monitored for readiness: {apps}")
        task = run_sync(update_status_background(self, apps, status))
        try:
            run_sync(
                self.jhelper.wait_until_active(
                    self.model,
                    apps,
                    timeout=OPENSTACK_DEPLOY_TIMEOUT,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug(str(e))
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class UpdateOpenStackModelConfigStep(BaseStep):
    """Update OpenStack ModelConfig via Terraform plan"""

    _CONFIG = CONFIG_KEY

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        manifest: Manifest,
        model_config: dict,
    ):
        super().__init__(
            "Update OpenStack model config",
            "Updating OpenStack model config related to proxy",
        )
        self.client = client
        self.tfhelper = tfhelper
        self.manifest = manifest
        self.model_config = model_config

    def run(self, status: Status | None = None) -> Result:
        """Apply model configs to openstack terraform plan."""
        try:
            self.model_config.update(
                {"workload-storage": K8SHelper.get_default_storageclass()}
            )
            override_tfvars = {"config": self.model_config}
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=override_tfvars,
                tf_apply_extra_args=["-target=juju_model.sunbeam"],
            )
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            LOG.exception("Error updating modelconfigs for openstack plan")
            return Result(ResultType.FAILED, str(e))
