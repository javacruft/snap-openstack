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

import ast
import builtins
import copy
import ipaddress
import logging
import ssl
import textwrap

from maas.client import bones
from rich.console import Console
from rich.status import Status
from snaphelpers import Snap

import sunbeam.commands.k8s as k8s
import sunbeam.commands.microceph as microceph
import sunbeam.commands.microk8s as microk8s
import sunbeam.jobs.questions
import sunbeam.provider.maas.client as maas_client
import sunbeam.provider.maas.deployment as maas_deployment
import sunbeam.utils as sunbeam_utils
from sunbeam.clusterd.client import Client
from sunbeam.commands.clusterd import APPLICATION as CLUSTERD_APPLICATION
from sunbeam.commands.configure import (
    CLOUD_CONFIG_SECTION,
    VARIABLE_DEFAULTS,
    SetHypervisorUnitsOptionsStep,
    ext_net_questions,
)
from sunbeam.commands.juju import (
    JUJU_CONTROLLER_CHARM,
    BootstrapJujuStep,
    ControllerNotFoundException,
    JujuStepHelper,
    ScaleJujuStep,
)
from sunbeam.commands.terraform import TerraformHelper
from sunbeam.jobs.checks import Check, DiagnosticsCheck, DiagnosticsResult
from sunbeam.jobs.common import (
    RAM_4_GB_IN_MB,
    RAM_32_GB_IN_MB,
    BaseStep,
    Result,
    ResultType,
)
from sunbeam.jobs.deployments import DeploymentsConfig
from sunbeam.jobs.juju import (
    ActionFailedException,
    JujuController,
    JujuHelper,
    LeaderNotFoundException,
    TimeoutException,
    UnitNotFoundException,
    run_sync,
)
from sunbeam.jobs.manifest import Manifest
from sunbeam.versions import JUJU_BASE

LOG = logging.getLogger(__name__)
console = Console()

ROLES_NEEDED_ERROR = f"""A machine needs roles to be a part of an openstack deployment.
Available roles are: {maas_deployment.RoleTags.values()}.
Roles can be assigned to a machine by applying tags in MAAS.
More on assigning tags: https://maas.io/docs/using-machine-tags
"""


class AddMaasDeployment(BaseStep):
    def __init__(
        self,
        deployments_config: DeploymentsConfig,
        deployment: maas_deployment.MaasDeployment,
    ) -> None:
        super().__init__(
            "Add MAAS-backed deployment",
            "Adding MAAS-backed deployment for OpenStack usage",
        )
        self.deployments_config = deployments_config
        self.deployment = deployment

    def is_skip(self, status: Status | None = None) -> Result:
        """Check if deployment is already added."""

        try:
            self.deployments_config.get_deployment(self.deployment.name)
            return Result(
                ResultType.FAILED, f"Deployment {self.deployment.name} already exists."
            )
        except ValueError:
            pass

        current_deployments = set()
        for deployment in self.deployments_config.deployments:
            if maas_deployment.is_maas_deployment(deployment):
                current_deployments.add(
                    (
                        deployment.url,
                        deployment.resource_pool,
                    )
                )

        if (self.deployment.url, self.deployment.resource_pool) in current_deployments:
            return Result(
                ResultType.FAILED,
                "Deployment with same url and resource pool already exists.",
            )

        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None = None) -> Result:
        """Check MAAS is working, Resource Pool exists, write to local configuration."""
        try:
            client = maas_client.MaasClient(self.deployment.url, self.deployment.token)
            _ = client.get_resource_pool(self.deployment.resource_pool)
        except ValueError as e:
            LOG.debug("Failed to connect to maas", exc_info=True)
            return Result(ResultType.FAILED, str(e))
        except bones.CallError as e:
            if e.status == 401:
                LOG.debug("Unauthorized", exc_info=True)
                return Result(
                    ResultType.FAILED,
                    "Unauthorized, check your api token has necessary permissions.",
                )
            elif e.status == 404:
                LOG.debug("Resource pool not found", exc_info=True)
                return Result(
                    ResultType.FAILED,
                    f"Resource pool {self.deployment.resource_pool!r} not"
                    " found in given MAAS URL.",
                )
            LOG.debug("Unknown error", exc_info=True)
            return Result(ResultType.FAILED, f"Unknown error, {e}")
        except Exception as e:
            match type(e.__cause__):
                case builtins.ConnectionRefusedError:
                    LOG.debug("Connection refused", exc_info=True)
                    return Result(
                        ResultType.FAILED, "Connection refused, is the url correct?"
                    )
                case ssl.SSLError:
                    LOG.debug("SSL error", exc_info=True)
                    return Result(
                        ResultType.FAILED, "SSL error, failed to connect to remote."
                    )
            LOG.info("Exception info", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        spaces = self.deployment.network_mapping.values()
        spaces = [space for space in spaces if space is not None]
        if len(spaces) > 0:
            try:
                maas_spaces = maas_client.list_spaces(client)
            except ValueError as e:
                LOG.debug("Failed to list spaces", exc_info=True)
                return Result(ResultType.FAILED, str(e))
            maas_spaces = [maas_space["name"] for maas_space in maas_spaces]
            difference = set(spaces).difference(maas_spaces)
            if len(difference) > 0:
                return Result(
                    ResultType.FAILED,
                    f"Spaces {', '.join(difference)} not found in MAAS.",
                )

        if (
            self.deployment.juju_controller is None
            and self.deployment.juju_account is not None  # noqa: W503
        ):
            return Result(
                ResultType.FAILED,
                "Juju account configured, but Juju Controller not configured.",
            )

        if (
            self.deployment.juju_controller is not None
            and self.deployment.juju_account is None  # noqa: W503
        ):
            return Result(
                ResultType.FAILED,
                "Juju Controller configured, but Juju account not configured.",
            )

        if (
            self.deployment.juju_account is not None
            and self.deployment.juju_controller is not None  # noqa: W503
        ):
            controller = self.deployment.get_connected_controller()
            try:
                run_sync(controller.list_models())
            except Exception as e:
                LOG.debug("Failed to list models", exc_info=True)
                return Result(ResultType.FAILED, str(e))

        self.deployments_config.add_deployment(self.deployment)
        return Result(ResultType.COMPLETED)


class MachineRolesCheck(DiagnosticsCheck):
    """Check machine has roles assigned."""

    def __init__(self, machine: dict):
        super().__init__(
            "Role check",
            "Checking roles",
        )
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        assigned_roles = self.machine["roles"]
        LOG.debug(f"{self.machine['hostname']=!r} assigned roles: {assigned_roles!r}")
        if not assigned_roles:
            return DiagnosticsResult(
                self.name,
                False,
                "machine has no role assigned.",
                diagnostics=ROLES_NEEDED_ERROR,
                machine=self.machine["hostname"],
            )

        return DiagnosticsResult(
            self.name,
            True,
            ", ".join(self.machine["roles"]),
            machine=self.machine["hostname"],
        )


class MachineNetworkCheck(DiagnosticsCheck):
    """Check machine has the right networks assigned."""

    def __init__(self, deployment: maas_deployment.MaasDeployment, machine: dict):
        super().__init__(
            "Network check",
            "Checking networks",
        )
        self.deployment = deployment
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        """Check machine has access to required networks."""
        network_to_space_mapping = maas_client.get_network_mapping(self.deployment)
        spaces = network_to_space_mapping.values()
        if len(spaces) != len(maas_deployment.Networks.values()) or not all(spaces):
            return DiagnosticsResult.fail(
                self.name,
                "network mapping is incomplete",
                diagnostics=textwrap.dedent(
                    """\
                    A complete map of networks to spaces is required to proceed.
                    Complete network mapping to using `sunbeam deployment space map...`.
                    """
                ),
                machine=self.machine["hostname"],
            )
        assigned_roles = self.machine["roles"]
        LOG.debug(f"{self.machine['hostname']=!r} assigned roles: {assigned_roles!r}")
        if not assigned_roles:
            return DiagnosticsResult.fail(
                self.name,
                "machine has no role assigned",
                diagnostics=ROLES_NEEDED_ERROR,
                machine=self.machine["hostname"],
            )
        assigned_spaces = self.machine["spaces"]
        LOG.debug(f"{self.machine['hostname']=!r} assigned spaces: {assigned_spaces!r}")
        required_networks: set[maas_deployment.Networks] = set()
        for role in assigned_roles:
            required_networks.update(
                maas_deployment.ROLE_NETWORK_MAPPING[maas_deployment.RoleTags(role)]
            )
        LOG.debug(
            f"{self.machine['hostname']=!r} required networks: {required_networks!r}"
        )
        required_spaces = set()
        missing_spaces = set()
        for network in required_networks:
            corresponding_space = network_to_space_mapping[network.value]
            required_spaces.add(corresponding_space)
            if corresponding_space not in assigned_spaces:
                missing_spaces.add(corresponding_space)
        LOG.debug(f"{self.machine['hostname']=!r} missing spaces: {missing_spaces!r}")
        if not assigned_spaces or missing_spaces:
            return DiagnosticsResult.fail(
                self.name,
                f"missing {', '.join(missing_spaces)}",
                diagnostics=textwrap.dedent(
                    f"""\
                    A machine needs to be in spaces to be a part of an openstack
                    deployment. Given machine has roles: {', '.join(assigned_roles)},
                    and therefore needs to be a part of the following spaces:
                    {', '.join(required_spaces)}."""
                ),
                machine=self.machine["hostname"],
            )
        return DiagnosticsResult.success(
            self.name,
            ", ".join(assigned_spaces),
            machine=self.machine["hostname"],
        )


class MachineStorageCheck(DiagnosticsCheck):
    """Check machine has storage assigned if required."""

    def __init__(self, machine: dict):
        super().__init__(
            "Storage check",
            "Checking storage",
        )
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        """Check machine has storage assigned if required."""
        assigned_roles = self.machine["roles"]
        LOG.debug(f"{self.machine['hostname']=!r} assigned roles: {assigned_roles!r}")
        if not assigned_roles:
            return DiagnosticsResult.fail(
                self.name,
                "machine has no role assigned.",
                ROLES_NEEDED_ERROR,
                machine=self.machine["hostname"],
            )
        if maas_deployment.RoleTags.STORAGE.value not in assigned_roles:
            self.message = "not a storage node."
            return DiagnosticsResult.success(
                self.name,
                self.message,
                machine=self.machine["hostname"],
            )
        # TODO(gboutry): check number of storage ?
        ceph_storage = self.machine["storage"].get(
            maas_deployment.StorageTags.CEPH.value, []
        )
        if len(ceph_storage) < 1:
            return DiagnosticsResult.fail(
                self.name,
                "storage node has no ceph storage",
                textwrap.dedent(
                    f"""\
                    A storage node needs to have ceph storage to be a part of
                    an openstack deployment. Either add ceph storage to the
                    machine or remove the storage role. Add the tag
                    `{maas_deployment.StorageTags.CEPH.value}` to the storage device in\
                     MAAS.
                    More on assigning tags: https://maas.io/docs/using-storage-tags"""
                ),
                machine=self.machine["hostname"],
            )
        return DiagnosticsResult.success(
            self.name,
            ", ".join(
                f"{tag}({len(devices)})"
                for tag, devices in self.machine["storage"].items()
            ),
            machine=self.machine["hostname"],
        )


class MachineComputeNicCheck(DiagnosticsCheck):
    """Check machine has compute nic assigned if required."""

    def __init__(self, machine: dict):
        super().__init__(
            "Compute Nic check",
            "Checking compute nic",
        )
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        """Check machine has compute nic if required."""
        assigned_roles = self.machine["roles"]
        LOG.debug(f"{self.machine['hostname']=!r} assigned roles: {assigned_roles!r}")
        if not assigned_roles:
            return DiagnosticsResult.fail(
                self.name,
                "machine has no role assigned.",
                ROLES_NEEDED_ERROR,
                machine=self.machine["hostname"],
            )
        compute_tag = maas_deployment.NicTags.COMPUTE.value
        if compute_tag not in assigned_roles:
            self.message = "not a compute node."
            return DiagnosticsResult.success(
                self.name,
                self.message,
                machine=self.machine["hostname"],
            )
        nics = self.machine["nics"]
        for nic in nics:
            if compute_tag in nic["tags"]:
                return DiagnosticsResult.success(
                    self.name,
                    "compute nic found",
                    machine=self.machine["hostname"],
                )

        return DiagnosticsResult.fail(
            self.name,
            "no compute nic found",
            textwrap.dedent(
                f"""\
                A compute node needs to have a dedicated nic for compute to be a part
                of an openstack deployment. Either add a compute nic to the machine or
                remove the compute role. Add the tag `{compute_tag}`
                to the nic in MAAS.
                More on assigning tags: https://maas.io/docs/using-network-tags
                """
            ),
            machine=self.machine["hostname"],
        )


class MachineRootDiskCheck(DiagnosticsCheck):
    """Check machine's root disk is a SSD and is large enough."""

    def __init__(self, machine: dict):
        super().__init__(
            "Root disk check",
            "Checking root disk",
        )
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        """Check machine's root disk is a SSD and is large enough."""
        root_disk = self.machine.get("root_disk")

        if root_disk is None:
            return DiagnosticsResult.fail(
                self.name,
                "could not determine root disk",
                "A machine needs to have a root disk to be a"
                " part of an openstack deployment.",
                machine=self.machine["hostname"],
            )

        if "ssd" not in root_disk["tags"]:
            return DiagnosticsResult.fail(
                self.name,
                "root disk is not a SSD",
                "A machine root disk needs to be an SSD to be"
                " a part of an openstack deployment. Deploying "
                "without SSD root disk can lead to performance issues.",
                machine=self.machine["hostname"],
            )

        if root_disk["root_partition"]["size"] < 500 * 1024**3:
            return DiagnosticsResult.fail(
                self.name,
                "root disk is too small",
                "A machine root disk needs to be at least 500GB"
                " to be a part of an openstack deployment.",
                machine=self.machine["hostname"],
            )

        return DiagnosticsResult.success(
            self.name,
            "root disk is a SSD and is large enough",
            machine=self.machine["hostname"],
        )


class MachineRequirementsCheck(DiagnosticsCheck):
    """Check machine meets requirements."""

    def __init__(self, machine: dict):
        super().__init__(
            "Machine requirements check",
            "Checking machine requirements",
        )
        self.machine = machine

    def run(self) -> DiagnosticsResult:
        """Check machine meets requirements."""
        if [maas_deployment.RoleTags.JUJU_CONTROLLER.value] == self.machine["roles"]:
            memory_min = RAM_4_GB_IN_MB
            core_min = 2
        else:
            memory_min = RAM_32_GB_IN_MB
            core_min = 16
        if self.machine["memory"] < memory_min or self.machine["cores"] < core_min:
            return DiagnosticsResult.fail(
                self.name,
                "machine does not meet requirements",
                textwrap.dedent(
                    f"""\
                    A machine needs to have at least {core_min} cores and
                    {memory_min}MB RAM to be a part of an openstack deployment.
                    Either add more cores and memory to the machine or remove the
                    machine from the deployment.
                    {self.machine['hostname']}:
                        roles: {self.machine["roles"]}
                        cores: {self.machine["cores"]}
                        memory: {self.machine["memory"]}MB"""
                ),
                machine=self.machine["hostname"],
            )

        return DiagnosticsResult.success(
            self.name,
            f"{self.machine['cores']} cores, {self.machine['memory']}MB RAM",
            machine=self.machine["hostname"],
        )


def _run_check_list(checks: list[DiagnosticsCheck]) -> list[DiagnosticsResult]:
    check_results = []
    for check in checks:
        LOG.debug(f"Starting check {check.name}")
        results = check.run()
        if isinstance(results, DiagnosticsResult):
            results = [results]
        for result in results:
            LOG.debug(f"{result.name=!r}, {result.passed=!r}, {result.message=!r}")
            check_results.extend(results)
    return check_results


class DeploymentMachinesCheck(DiagnosticsCheck):
    """Check all machines inside deployment."""

    def __init__(
        self, deployment: maas_deployment.MaasDeployment, machines: list[dict]
    ):
        super().__init__(
            "Deployment check",
            "Checking machines, roles, networks and storage",
        )
        self.deployment = deployment
        self.machines = machines

    def run(self) -> list[DiagnosticsResult]:
        """Run a series of checks on the machines' definition."""
        checks = []
        for machine in self.machines:
            checks.append(MachineRolesCheck(machine))
            checks.append(MachineNetworkCheck(self.deployment, machine))
            checks.append(MachineStorageCheck(machine))
            checks.append(MachineComputeNicCheck(machine))
            checks.append(MachineRootDiskCheck(machine))
            checks.append(MachineRequirementsCheck(machine))
        results = _run_check_list(checks)
        results.append(
            DiagnosticsResult(self.name, all(result.passed for result in results))
        )
        return results


class DeploymentRolesCheck(DiagnosticsCheck):
    """Check deployment as enough nodes with given role."""

    def __init__(
        self, machines: list[dict], role_name: str, role_tag: str, min_count: int = 3
    ):
        super().__init__(
            "Minimum role check",
            "Checking minimum number of machines with given role",
        )
        self.machines = machines
        self.role_name = role_name
        self.role_tag = role_tag
        self.min_count = min_count

    def run(self) -> DiagnosticsResult:
        """Checks if there's enough machines with given role."""
        machines = 0
        for machine in self.machines:
            if self.role_tag in machine["roles"]:
                machines += 1
        if machines < self.min_count:
            return DiagnosticsResult.fail(
                self.name,
                "less than 3 " + self.role_name,
                textwrap.dedent(
                    """\
                    A deployment needs to have at least {min_count} {role_name} to be
                    a part of an openstack deployment. You need to add more {role_name}
                    to the deployment using {role_tag} tag.
                    More on using tags: https://maas.io/docs/using-machine-tags
                    """.format(
                        min_count=self.min_count,
                        role_name=self.role_name,
                        role_tag=self.role_tag,
                    )
                ),
            )
        return DiagnosticsResult.success(
            self.name,
            f"{self.role_name}: {machines}",
        )


class ZonesCheck(DiagnosticsCheck):
    """Check that there either 1 zone or more than 2 zones."""

    def __init__(self, zones: list[str]):
        super().__init__(
            "Zone check",
            "Checking zones",
        )
        self.zones = zones

    def run(self) -> DiagnosticsResult:
        """Checks deployment zones."""
        if len(self.zones) == 2:
            return DiagnosticsResult.fail(
                self.name,
                "deployment has 2 zones",
                textwrap.dedent(
                    f"""\
                    A deployment needs to have either 1 zone or more than 2 zones.
                    Current zones: {', '.join(self.zones)}"""
                ),
            )
        return DiagnosticsResult.success(
            self.name,
            f"{len(self.zones)} zone(s)",
        )


class ZoneBalanceCheck(DiagnosticsCheck):
    """Check that roles are balanced throughout zones."""

    def __init__(self, machines: dict[str, list[dict]]):
        super().__init__(
            "Zone balance check",
            "Checking role distribution across zones",
        )
        self.machines = machines

    def run(self) -> DiagnosticsResult:
        """Check role distribution across zones."""
        zone_role_counts = {}
        for zone, machines in self.machines.items():
            zone_role_counts.setdefault(zone, {})
            for machine in machines:
                for role in machine["roles"]:
                    zone_role_counts[zone].setdefault(role, 0)
                    zone_role_counts[zone][role] += 1
        LOG.debug(f"{zone_role_counts=!r}")
        unbalanced_roles = []
        distribution = ""
        for role in maas_deployment.RoleTags.values():
            counts = [zone_role_counts[zone].get(role, 0) for zone in zone_role_counts]
            max_count = max(counts)
            min_count = min(counts)
            if max_count != min_count:
                unbalanced_roles.append(role)
            distribution += f"{role}:"
            for zone, counts in zone_role_counts.items():
                distribution += f"\n  {zone}={counts.get(role, 0)}"
            distribution += "\n"

        if unbalanced_roles:
            diagnostics = textwrap.dedent(
                """\
                A deployment needs to have the same number of machines with the same
                role in each zone. Either add more machines to the zones or remove the
                zone from the deployment.
                More on using tags: https://maas.io/docs/using-machine-tags
                Distribution of roles across zones:
                """
            )
            diagnostics += distribution
            return DiagnosticsResult.fail(
                self.name,
                f"{', '.join(unbalanced_roles)} distribution is unbalanced",
                diagnostics,
            )
        return DiagnosticsResult.success(
            self.name,
            "deployment is balanced",
            distribution,
        )


class IpRangesCheck(DiagnosticsCheck):
    """Check IP ranges are complete."""

    _missing_range_diagnostic = textwrap.dedent(
        """\
        IP ranges are required to proceed.
        You need to setup a Reserverd IP Range for any subnet in the
        space ({space!r}) mapped to the {network!r} network.
        Multiple ip ranges can be defined in on the same/different subnets
        in the space. Each of these ip ranges should have as comment:
        {label!r}.

        More on setting up IP ranges:
        https://maas.io/docs/ip-ranges
        """
    )

    def __init__(
        self,
        client: maas_client.MaasClient,
        deployment: maas_deployment.MaasDeployment,
    ):
        super().__init__(
            "IP ranges check",
            "Checking IP ranges",
        )
        self.client = client
        self.deployment = deployment

    def _get_ranges_for_label(
        self, subnet_ranges: dict[str, list[dict]], label: str
    ) -> list[tuple]:
        """Ip ranges for a given label."""
        ip_ranges = []
        for ranges in subnet_ranges.values():
            for ip_range in ranges:
                if ip_range["label"] == label:
                    ip_ranges.append((ip_range["start"], ip_range["end"]))

        return ip_ranges

    def run(
        self,
    ) -> DiagnosticsResult:
        """Check Public and Internal ip ranges are set."""

        public_space = self.deployment.network_mapping.get(
            maas_deployment.Networks.PUBLIC.value
        )
        internal_space = self.deployment.network_mapping.get(
            maas_deployment.Networks.INTERNAL.value
        )
        if public_space is None or internal_space is None:
            return DiagnosticsResult.fail(
                self.name,
                "IP ranges are not set",
                textwrap.dedent(
                    """\
                    A complete map of networks to spaces is required to proceed.
                    Complete network mapping to using `sunbeam deployment space map...`.
                    """
                ),
            )

        public_subnet_ranges = maas_client.get_ip_ranges_from_space(
            self.client, public_space
        )
        internal_subnet_ranges = maas_client.get_ip_ranges_from_space(
            self.client, internal_space
        )

        public_ip_ranges = self._get_ranges_for_label(
            public_subnet_ranges, self.deployment.public_api_label
        )
        internal_ip_ranges = self._get_ranges_for_label(
            internal_subnet_ranges, self.deployment.internal_api_label
        )
        if len(public_ip_ranges) == 0:
            return DiagnosticsResult.fail(
                self.name,
                "Public IP ranges are not set",
                self._missing_range_diagnostic.format(
                    space=public_space,
                    network=maas_deployment.Networks.PUBLIC.value,
                    label=self.deployment.public_api_label,
                ),
            )

        if len(internal_ip_ranges) == 0:
            return DiagnosticsResult.fail(
                self.name,
                "Internal IP ranges are not set",
                self._missing_range_diagnostic.format(
                    space=internal_space,
                    network=maas_deployment.Networks.INTERNAL.value,
                    label=self.deployment.internal_api_label,
                ),
            )

        diagnostics = (
            f"Public IP ranges: {public_ip_ranges!r}\n"
            f"Internal IP ranges: {internal_ip_ranges!r}"
        )

        return DiagnosticsResult.success(self.name, "IP ranges are set", diagnostics)


class DeploymentTopologyCheck(DiagnosticsCheck):
    """Check deployment topology."""

    def __init__(self, machines: list[dict]):
        super().__init__(
            "Topology check",
            "Checking zone distribution",
        )
        self.machines = machines

    def run(self) -> list[DiagnosticsResult]:
        """Run a sequence of checks to validate deployment topology.""" ""
        machines_by_zone = maas_client._group_machines_by_zone(self.machines)
        checks = []
        checks.append(
            DeploymentRolesCheck(
                self.machines,
                "juju controllers",
                maas_deployment.RoleTags.JUJU_CONTROLLER.value,
            )
        )
        checks.append(
            DeploymentRolesCheck(
                self.machines, "control nodes", maas_deployment.RoleTags.CONTROL.value
            )
        )
        checks.append(
            DeploymentRolesCheck(
                self.machines, "compute nodes", maas_deployment.RoleTags.COMPUTE.value
            )
        )
        checks.append(
            DeploymentRolesCheck(
                self.machines, "storage nodes", maas_deployment.RoleTags.STORAGE.value
            )
        )
        checks.append(ZonesCheck(list(machines_by_zone.keys())))
        checks.append(ZoneBalanceCheck(machines_by_zone))

        results = _run_check_list(checks)
        results.append(
            DiagnosticsResult(self.name, all(result.passed for result in results))
        )
        return results


class DeploymentNetworkingCheck(DiagnosticsCheck):
    """Check deployment networking."""

    def __init__(
        self,
        client: maas_client.MaasClient,
        deployment: maas_deployment.MaasDeployment,
    ):
        super().__init__(
            "Networking check",
            "Checking networking",
        )
        self.client = client
        self.deployment = deployment

    def run(self) -> list[DiagnosticsResult]:
        """Run a sequence of checks to validate deployment networking."""
        checks = []
        checks.append(IpRangesCheck(self.client, self.deployment))

        results = _run_check_list(checks)
        results.append(
            DiagnosticsResult(self.name, all(result.passed for result in results))
        )
        return results


class NetworkMappingCompleteCheck(Check):
    """Check network mapping is complete."""

    def __init__(self, deployment: maas_deployment.MaasDeployment):
        super().__init__(
            "NetworkMapping Check",
            "Checking network mapping is complete",
        )
        self.deployment = deployment

    def run(self) -> bool:
        """Check network mapping is complete."""
        network_to_space_mapping = self.deployment.network_mapping
        spaces = network_to_space_mapping.values()
        if len(spaces) != len(maas_deployment.Networks.values()) or not all(spaces):
            self.message = (
                "A complete map of networks to spaces is required to proceed."
                " Complete network mapping to using `sunbeam deployment space map...`."
            )
            return False
        return True


class MaasBootstrapJujuStep(BootstrapJujuStep):
    """Bootstrap the Juju controller."""

    def __init__(
        self,
        maas_client: maas_client.MaasClient,
        cloud: str,
        cloud_type: str,
        controller: str,
        password: str,
        bootstrap_args: list[str] | None = None,
        deployment_preseed: dict | None = None,
        proxy_settings: dict | None = None,
        accept_defaults: bool = False,
    ):
        bootstrap_args = bootstrap_args or []
        bootstrap_args.extend(
            (
                "--bootstrap-constraints",
                f"tags={maas_deployment.RoleTags.JUJU_CONTROLLER.value}",
                "--bootstrap-base",
                JUJU_BASE,
            )
        )
        if proxy_settings:
            snap = Snap()
            controller_charm = str(
                snap.paths.user_common / "downloads" / JUJU_CONTROLLER_CHARM
            )
            bootstrap_args.extend(
                (
                    "--controller-charm-path",
                    controller_charm,
                )
            )
        bootstrap_args.extend(
            (
                "--config",
                f"admin-secret={password}",
            )
        )
        super().__init__(
            # client is not used when bootstrapping with maas,
            # as it was used during prompts and there's no prompt with maas
            None,  # type: ignore
            cloud,
            cloud_type,
            controller,
            bootstrap_args=bootstrap_args,
            deployment_preseed=deployment_preseed,
            proxy_settings=proxy_settings,
            accept_defaults=accept_defaults,
        )
        self.maas_client = maas_client

    def prompt(self, console: Console | None = None) -> None:
        """Determines if the step can take input from the user.

        Prompts are used by Steps to gather the necessary input prior to
        running the step. Steps should not expect that the prompt will be
        available and should provide a reasonable default where possible.
        """

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user.

        :return: True if the step can ask the user for prompts,
                 False otherwise
        """
        return False

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        controller_tag = maas_deployment.RoleTags.JUJU_CONTROLLER.value
        machines = maas_client.list_machines(self.maas_client, tags=controller_tag)
        if len(machines) == 0:
            return Result(
                ResultType.FAILED,
                f"No machines with tag {controller_tag!r} found.",
            )
        controller = sorted(machines, key=lambda x: x["hostname"])[0]
        self.bootstrap_args.extend(("--to", "system-id=" + controller["system_id"]))
        return super().is_skip(status)


class MaasScaleJujuStep(ScaleJujuStep):
    """Scale Juju Controller on MAAS deployment."""

    def __init__(
        self,
        maas_client: maas_client.MaasClient,
        controller: str,
        extra_args: list[str] | None = None,
    ):
        extra_args = extra_args or []
        extra_args.extend(
            (
                "--constraints",
                f"tags={maas_deployment.RoleTags.JUJU_CONTROLLER.value}",
            )
        )
        super().__init__(controller, extra_args=extra_args)
        self.client = maas_client

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        try:
            controller = self.get_controller(self.controller)
        except ControllerNotFoundException as e:
            LOG.debug(str(e))
            return Result(ResultType.FAILED, f"Controller {self.controller} not found")

        controller_machines = controller.get("controller-machines")
        if controller_machines is None:
            return Result(
                ResultType.FAILED,
                f"Controller {self.controller} has no machines registered.",
            )
        nb_controllers = len(controller_machines)

        if nb_controllers == self.n:
            LOG.debug("Already the correct number of controllers, skipping scaling...")
            return Result(ResultType.SKIPPED)

        if nb_controllers > self.n:
            return Result(
                ResultType.FAILED,
                f"Can't scale down controllers from {nb_controllers} to {self.n}.",
            )

        machines = maas_client.list_machines(
            self.client, tags=maas_deployment.RoleTags.JUJU_CONTROLLER.value
        )

        if len(machines) < self.n:
            LOG.debug(
                f"Found {len(machines)} juju controllers,"
                f" need {self.n} to scale, skipping..."
            )
            return Result(ResultType.SKIPPED)
        machines = sorted(machines, key=lambda x: x["hostname"])

        system_ids = [machine["system_id"] for machine in machines]
        for controller_machine in controller_machines.values():
            if controller_machine["instance-id"] in system_ids:
                system_ids.remove(controller_machine["instance-id"])

        placement = ",".join(f"system-id={system_id}" for system_id in system_ids)

        self.extra_args.extend(("--to", placement))
        return Result(ResultType.COMPLETED)


class MaasSaveControllerStep(BaseStep, JujuStepHelper):
    """Save maas controller information locally."""

    def __init__(
        self,
        controller: str,
        deployment_name: str,
        deployments_config: DeploymentsConfig,
    ):
        super().__init__(
            "Save controller information",
            "Saving controller information locally",
        )
        self.controller = controller
        self.deployment_name = deployment_name
        self.deployments_config = deployments_config

    def _get_controller(self, name: str) -> JujuController | None:
        try:
            controller = self.get_controller(name)["details"]
        except ControllerNotFoundException as e:
            LOG.debug(str(e))
            return None
        return JujuController(
            api_endpoints=controller["api-endpoints"],
            ca_cert=controller["ca-cert"],
        )

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        deployment = self.deployments_config.get_deployment(self.deployment_name)
        if not maas_deployment.is_maas_deployment(deployment):
            return Result(ResultType.SKIPPED)
        if deployment.juju_controller is None:
            return Result(ResultType.COMPLETED)

        controller = self._get_controller(self.controller)
        if controller is None:
            return Result(ResultType.FAILED, f"Controller {self.controller} not found")

        if controller == deployment.juju_controller:
            return Result(ResultType.SKIPPED)

        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None) -> Result:
        """Save controller to deployment information."""
        controller = self._get_controller(self.controller)
        if controller is None:
            return Result(ResultType.FAILED, f"Controller {self.controller} not found")

        deployment = self.deployments_config.get_deployment(self.deployment_name)
        if not maas_deployment.is_maas_deployment(deployment):
            return Result(ResultType.FAILED)

        deployment.juju_controller = controller
        self.deployments_config.write()
        return Result(ResultType.COMPLETED)


class MaasSaveClusterdAddressStep(BaseStep):
    """Save clusterd address locally."""

    def __init__(
        self,
        jhelper: JujuHelper,
        deployment_name: str,
        deployments_config: DeploymentsConfig,
    ):
        super().__init__(
            "Save clusterd address",
            "Saving clusterd address locally",
        )
        self.jhelper = jhelper
        self.deployment_name = deployment_name
        self.deployments_config = deployments_config

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        deployment = self.deployments_config.get_deployment(self.deployment_name)
        if not maas_deployment.is_maas_deployment(deployment):
            return Result(ResultType.SKIPPED)
        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None) -> Result:
        """Save clusterd address to deployment information."""

        async def _get_credentials() -> dict:
            leader_unit = await self.jhelper.get_leader_unit(
                CLUSTERD_APPLICATION, "controller"
            )
            result = await self.jhelper.run_action(
                leader_unit, "controller", "get-credentials"
            )
            if result.get("return-code", 0) > 1:
                raise ValueError("Failed to retrieve credentials")
            return result

        try:
            credentials = run_sync(_get_credentials())
        except (LeaderNotFoundException, ValueError, ActionFailedException) as e:
            return Result(ResultType.FAILED, str(e))

        url = credentials.get("url")
        if url is None:
            return Result(ResultType.FAILED, "Failed to retrieve clusterd url")

        client = Client.from_http(url)
        try:
            client.cluster.list_nodes()
        except Exception as e:
            return Result(ResultType.FAILED, str(e))
        deployment = self.deployments_config.get_deployment(self.deployment_name)
        if not maas_deployment.is_maas_deployment(deployment):
            return Result(ResultType.FAILED)

        deployment.clusterd_address = url
        self.deployments_config.write()
        return Result(ResultType.COMPLETED)


class MaasAddMachinesToClusterdStep(BaseStep):
    """Add machines from MAAS to Clusterd."""

    def __init__(self, client: Client, maas_client: maas_client.MaasClient):
        super().__init__("Add machines", "Adding machines to Clusterd")
        self.client = client
        self.maas_client = maas_client
        self.machines = None
        self.nodes = None

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        maas_machines = maas_client.list_machines(self.maas_client)
        LOG.debug(f"Machines fetched: {maas_machines}")
        filtered_machines = []
        for machine in maas_machines:
            if set(machine["roles"]).intersection(
                {
                    maas_deployment.RoleTags.CONTROL.value,
                    maas_deployment.RoleTags.COMPUTE.value,
                    maas_deployment.RoleTags.STORAGE.value,
                }
            ):
                filtered_machines.append(machine)
        LOG.debug(f"Machines containing worker roles: {filtered_machines}")
        if filtered_machines is None or len(filtered_machines) == 0:
            return Result(ResultType.FAILED, "Maas deployment has no machines.")
        clusterd_nodes = self.client.cluster.list_nodes()
        nodes_to_update = []
        for node in clusterd_nodes:
            for machine in filtered_machines:
                if node["name"] == machine["hostname"]:
                    filtered_machines.remove(machine)
                    if sorted(node["role"]) != sorted(machine["roles"]):
                        nodes_to_update.append((machine["hostname"], machine["roles"]))
        self.nodes = nodes_to_update
        self.machines = filtered_machines
        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None = None) -> Result:
        """Add machines to Juju model."""
        if self.machines is None or self.nodes is None:
            # only happens if is_skip() was not called before, or if run executed
            # even if is_skip reported a failure
            return Result(ResultType.FAILED, "No machines to add / node to update.")
        for machine in self.machines:
            self.client.cluster.add_node_info(
                machine["hostname"], machine["roles"], systemid=machine["system_id"]
            )
        for node in self.nodes:
            self.client.cluster.update_node_info(*node)
        return Result(ResultType.COMPLETED)


class MaasDeployMachinesStep(BaseStep):
    """Deploy machines stored in Clusterd in Juju."""

    def __init__(self, client: Client, jhelper: JujuHelper, model: str):
        super().__init__("Deploy machines", "Deploying machines in Juju")
        self.client = client
        self.jhelper = jhelper
        self.model = model

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        clusterd_nodes = self.client.cluster.list_nodes()
        if len(clusterd_nodes) == 0:
            return Result(ResultType.FAILED, "No machines to deploy.")

        juju_machines = run_sync(self.jhelper.get_machines(self.model))

        nodes_to_deploy = clusterd_nodes.copy()
        nodes_to_update = []
        for node in clusterd_nodes:
            node_machine_id = node["machineid"]
            for id, machine in juju_machines.items():
                if node["name"] == machine.hostname:
                    if int(id) != node_machine_id and node_machine_id != -1:
                        return Result(
                            ResultType.FAILED,
                            f"Machine {node['name']} already exists in model"
                            f" {self.model} with id {id},"
                            f" expected the id {node['machineid']}.",
                        )
                    if (
                        node["systemid"] != machine.instance_id
                        and node["systemid"] != ""  # noqa: W503
                    ):
                        return Result(
                            ResultType.FAILED,
                            f"Machine {node['name']} already exists in model"
                            f" {self.model} with systemid {machine.instance_id},"
                            f" expected the systemid {node['systemid']}.",
                        )
                    if node_machine_id == -1:
                        nodes_to_update.append(node)
                    nodes_to_deploy.remove(node)
                    break

        self.nodes_to_deploy = sorted(nodes_to_deploy, key=lambda x: x["name"])
        self.nodes_to_update = nodes_to_update

        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None = None) -> Result:
        """Deploy machines in Juju."""
        for node in self.nodes_to_deploy:
            self.update_status(status, f"deploying {node['name']}")
            LOG.debug(f"Adding machine {node['name']} to model {self.model}")
            juju_machine = run_sync(
                self.jhelper.add_machine("system-id=" + node["systemid"], self.model)
            )
            self.client.cluster.update_node_info(
                node["name"], machineid=int(juju_machine.id)
            )
        self.update_status(status, "waiting for machines to deploy")
        model = run_sync(self.jhelper.get_model(self.model))
        for node in self.nodes_to_update:
            LOG.debug(f"Updating machine {node['name']} in model {self.model}")
            for juju_machine in model.machines.values():
                if juju_machine is None:
                    continue
                if juju_machine.hostname == node["name"]:
                    self.client.cluster.update_node_info(
                        node["name"], machineid=int(juju_machine.id)
                    )
                    break
        try:
            run_sync(self.jhelper.wait_all_machines_deployed(self.model))
        except TimeoutException:
            LOG.debug("Timeout waiting for machines to deploy", exc_info=True)
            return Result(ResultType.FAILED, "Timeout waiting for machines to deploy.")
        return Result(ResultType.COMPLETED)


class MaasConfigureMicrocephOSDStep(BaseStep):
    """Configure Microceph OSD disks"""

    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        jhelper: JujuHelper,
        names: list[str],
        model: str,
    ):
        super().__init__("Configure MicroCeph storage", "Configuring MicroCeph storage")
        self.client = client
        self.maas_client = maas_client
        self.jhelper = jhelper
        self.names = names
        self.model = model
        self.disks_to_configure: dict[str, list[str]] = {}

    async def _list_disks(self, unit: str) -> tuple[dict, dict]:
        """Call list-disks action on an unit."""
        LOG.debug("Running list-disks on : %r", unit)
        action_result = await self.jhelper.run_action(unit, self.model, "list-disks")
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

    async def _get_microceph_disks(self) -> dict:
        """Retrieve all disks added to microceph.

        Return a dict of format:
            {
                "<machine>": {
                    "osds": ["<disk1_path>", "<disk2_path>"],
                    "unpartitioned_disks": ["<disk3_path>"]
                    "unit": "<unit_name>"
                }
            }
        """
        try:
            leader = await self.jhelper.get_leader_unit(
                microceph.APPLICATION, self.model
            )
        except LeaderNotFoundException as e:
            LOG.debug("Failed to find leader unit", exc_info=True)
            raise ValueError(str(e))
        osds, _ = await self._list_disks(leader)
        disks = {}
        default_disk = {"osds": [], "unpartitioned_disks": []}
        for osd in osds:
            location = osd["location"]  # machine name
            disks.setdefault(location, copy.deepcopy(default_disk))["osds"].append(
                osd["path"]
            )

        for name in self.names:
            machine_id = str(self.client.cluster.get_node_info(name)["machineid"])
            unit = await self.jhelper.get_unit_from_machine(
                microceph.APPLICATION, machine_id, self.model
            )
            if unit is None:
                raise ValueError(
                    f"{microceph.APPLICATION}'s unit not found on {name}."
                    " Is microceph deployed on this machine?"
                )
            _, unit_unpartitioned_disks = await self._list_disks(unit.entity_id)
            disks.setdefault(name, copy.deepcopy(default_disk))[
                "unpartitioned_disks"
            ].extend(uud["path"] for uud in unit_unpartitioned_disks)
            disks[name]["unit"] = unit.entity_id

        return disks

    def _get_maas_disks(self) -> dict:
        """Retrieve all disks from MAAS per machine.

        Return a dict of format:
            {
                "<machine>": ["<disk1_path>", "<disk2_path>"]
            }
        """
        machines = maas_client.list_machines(self.maas_client, hostname=self.names)
        disks = {}
        for machine in machines:
            disks[machine["hostname"]] = [
                device["id_path"]
                for device in machine["storage"][maas_deployment.StorageTags.CEPH.value]
            ]

        return disks

    def _compute_disks_to_configure(
        self, microceph_disks: dict, maas_disks: set[str]
    ) -> list[str]:
        """Compute the disks that need to be configured for the machine."""
        machine_osds = set(microceph_disks["osds"])
        machine_unpartitioned_disks = set(microceph_disks["unpartitioned_disks"])
        machine_unit = microceph_disks["unit"]
        if len(maas_disks) == 0:
            raise ValueError(
                f"Machine {machine_unit!r} does not have any"
                f" {maas_deployment.StorageTags.CEPH.value!r} disk defined."
            )
        # Get all disks that are in Ceph but not in MAAS
        unknown_osds = machine_osds - maas_disks
        # Get all disks that are in MAAS but neither in Ceph nor unpartitioned
        missing_disks = maas_disks - machine_osds - machine_unpartitioned_disks
        # Disks to partition
        disks_to_configure = maas_disks.intersection(machine_unpartitioned_disks)

        if len(unknown_osds) > 0:
            raise ValueError(
                f"Machine {machine_unit!r} has OSDs from disks unknown to MAAS:"
                f" {unknown_osds}"
            )
        if len(missing_disks) > 0:
            raise ValueError(
                f"Machine {machine_unit!r} is missing disks: {missing_disks}"
            )
        if len(disks_to_configure) > 0:
            LOG.debug(
                "Unit %r will configure the following disks: %r",
                machine_unit,
                disks_to_configure,
            )
            return list(disks_to_configure)

        return []

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not."""
        try:
            microceph_disks = run_sync(self._get_microceph_disks())
            LOG.debug("Computing disk mapping: %r", microceph_disks)
        except ValueError as e:
            LOG.debug("Failed to list microceph disks from units", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        try:
            maas_disks = self._get_maas_disks()
            LOG.debug("Maas disks: %r", maas_disks)
        except ValueError as e:
            LOG.debug("Failed to list disks from MAAS", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        disks_to_configure: dict[str, list[str]] = {}

        for name in self.names:
            try:
                machine_disks_to_configure = self._compute_disks_to_configure(
                    microceph_disks[name], set(maas_disks.get(name, []))
                )
            except ValueError as e:
                LOG.debug(
                    "Failed to compute disks to configure for machine %r",
                    name,
                    exc_info=True,
                )
                return Result(ResultType.FAILED, str(e))
            if len(machine_disks_to_configure) > 0:
                disks_to_configure[microceph_disks[name]["unit"]] = (
                    machine_disks_to_configure
                )

        if len(disks_to_configure) == 0:
            LOG.debug("No disks to configure, skipping step.")
            return Result(ResultType.SKIPPED)

        self.disks_to_configure = disks_to_configure
        return Result(ResultType.COMPLETED)

    def run(self, status: Status | None = None) -> Result:
        """Configure local disks on microceph."""

        for unit, disks in self.disks_to_configure.items():
            try:
                LOG.debug("Running action add-osd on %r", unit)
                action_result = run_sync(
                    self.jhelper.run_action(
                        unit,
                        self.model,
                        "add-osd",
                        action_params={
                            "device-id": ",".join(disks),
                        },
                    )
                )
                LOG.debug(
                    "Result after running action add-osd on %r: %r", unit, action_result
                )
            except (UnitNotFoundException, ActionFailedException) as e:
                LOG.debug("Failed to run action add-osd on %r", unit, exc_info=True)
                return Result(ResultType.FAILED, str(e))
        return Result(ResultType.COMPLETED)


class MaasDeployMicrok8sApplicationStep(microk8s.DeployMicrok8sApplicationStep):
    """Deploy Microk8s application using Terraform"""

    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        public_space: str,
        public_label: str,
        internal_space: str,
        internal_label: str,
        model: str,
        deployment_preseed: dict | None = None,
        accept_defaults: bool = False,
    ):
        super().__init__(
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            deployment_preseed,
            accept_defaults,
        )
        self.maas_client = maas_client
        self.public_space = public_space
        self.public_label = public_label
        self.internal_space = internal_space
        self.internal_label = internal_label
        self.ranges = None

    def extra_tfvars(self) -> dict:
        if self.ranges is None:
            raise ValueError("No ip ranges found")
        return {"addons": {"dns": "", "hostpath-storage": "", "metallb": self.ranges}}

    def prompt(self, console: Console | None = None) -> None:
        """Determines if the step can take input from the user.

        Prompts are used by Steps to gather the necessary input prior to
        running the step. Steps should not expect that the prompt will be
        available and should provide a reasonable default where possible.
        """

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user.

        :return: True if the step can ask the user for prompts,
                 False otherwise
        """
        return False

    def _to_joined_range(self, subnet_ranges: dict[str, list[dict]], label: str) -> str:
        """Convert a list of ip ranges to a string for cni config.

        Current cni config format is: <ip start>-<ip end>,<ip  start>-<ip end>,...
        """
        mettallb_range = []
        for ip_ranges in subnet_ranges.values():
            for ip_range in ip_ranges:
                if ip_range["label"] == label:
                    mettallb_range.append(f"{ip_range['start']}-{ip_range['end']}")
        if len(mettallb_range) == 0:
            raise ValueError("No ip range found for label: " + label)
        return ",".join(mettallb_range)

    def is_skip(self, status: Status | None = None):
        """Determines if the step should be skipped or not."""
        try:
            public_ranges = maas_client.get_ip_ranges_from_space(
                self.maas_client, self.public_space
            )
            LOG.debug("Public ip ranges: %r", public_ranges)
        except ValueError as e:
            LOG.debug(
                "Failed to ip ranges for space: %r", self.public_space, exc_info=True
            )
            return Result(ResultType.FAILED, str(e))
        try:
            public_metallb_range = self._to_joined_range(
                public_ranges, self.public_label
            )
        except ValueError:
            LOG.debug(
                "No iprange with label %r found",
                self.public_label,
                exc_info=True,
            )
            return Result(ResultType.FAILED, "No public ip range found")
        self.ranges = public_metallb_range

        try:
            internal_ranges = maas_client.get_ip_ranges_from_space(
                self.maas_client, self.internal_space
            )
            LOG.debug("Internal ip ranges: %r", internal_ranges)
        except ValueError as e:
            LOG.debug(
                "Failed to ip ranges for space: %r", self.internal_space, exc_info=True
            )
            return Result(ResultType.FAILED, str(e))
        try:
            # TODO(gboutry): use this range when cni (or sunbeam) easily supports
            # using different ip pools
            internal_metallb_range = self._to_joined_range(  # noqa
                internal_ranges, self.internal_label
            )
        except ValueError:
            LOG.debug(
                "No iprange with label %r found",
                self.internal_label,
                exc_info=True,
            )
            return Result(ResultType.FAILED, "No internal ip range found")

        return super().is_skip(status)


class MaasDeployK8SApplicationStep(k8s.DeployK8SApplicationStep):
    """Deploy K8S application using Terraform"""

    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        public_space: str,
        public_label: str,
        internal_space: str,
        internal_label: str,
        model: str,
        deployment_preseed: dict | None = None,
        accept_defaults: bool = False,
    ):
        super().__init__(
            client,
            tfhelper,
            jhelper,
            manifest,
            model,
            deployment_preseed,
            accept_defaults,
        )
        self.maas_client = maas_client
        self.public_space = public_space
        self.public_label = public_label
        self.internal_space = internal_space
        self.internal_label = internal_label
        self.ranges = None

    def extra_tfvars(self) -> dict:
        return {}

    def prompt(self, console: Console | None = None) -> None:
        """Determines if the step can take input from the user.

        Prompts are used by Steps to gather the necessary input prior to
        running the step. Steps should not expect that the prompt will be
        available and should provide a reasonable default where possible.
        """

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user.

        :return: True if the step can ask the user for prompts,
                 False otherwise
        """
        return False

    def _to_joined_range(self, subnet_ranges: dict[str, list[dict]], label: str) -> str:
        """Convert a list of ip ranges to a string for cni config.

        Current cni config format is: <ip start>-<ip end>,<ip  start>-<ip end>,...
        """
        lb_range = []
        for ip_ranges in subnet_ranges.values():
            for ip_range in ip_ranges:
                if ip_range["label"] == label:
                    lb_range.append(f"{ip_range['start']}-{ip_range['end']}")
        if len(lb_range) == 0:
            raise ValueError("No ip range found for label: " + label)
        return ",".join(lb_range)

    def is_skip(self, status: Status | None = None):
        """Determines if the step should be skipped or not."""
        try:
            public_ranges = maas_client.get_ip_ranges_from_space(
                self.maas_client, self.public_space
            )
            LOG.debug("Public ip ranges: %r", public_ranges)
        except ValueError as e:
            LOG.debug(
                "Failed to ip ranges for space: %r", self.public_space, exc_info=True
            )
            return Result(ResultType.FAILED, str(e))
        try:
            public_metallb_range = self._to_joined_range(
                public_ranges, self.public_label
            )
        except ValueError:
            LOG.debug(
                "No iprange with label %r found",
                self.public_label,
                exc_info=True,
            )
            return Result(ResultType.FAILED, "No public ip range found")
        self.ranges = public_metallb_range

        try:
            internal_ranges = maas_client.get_ip_ranges_from_space(
                self.maas_client, self.internal_space
            )
            LOG.debug("Internal ip ranges: %r", internal_ranges)
        except ValueError as e:
            LOG.debug(
                "Failed to ip ranges for space: %r", self.internal_space, exc_info=True
            )
            return Result(ResultType.FAILED, str(e))
        try:
            # TODO(gboutry): use this range when cni (or sunbeam) easily supports
            # using different ip pools
            internal_metallb_range = self._to_joined_range(  # noqa
                internal_ranges, self.internal_label
            )
        except ValueError:
            LOG.debug(
                "No iprange with label %r found",
                self.internal_label,
                exc_info=True,
            )
            return Result(ResultType.FAILED, "No internal ip range found")

        return super().is_skip(status)


class MaasEnableK8SFeatures(k8s.EnableK8SFeatures):
    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        jhelper: JujuHelper,
        public_space: str,
        public_label: str,
        model: str,
    ):
        super().__init__(
            client,
            jhelper,
            model,
        )
        self.maas_client = maas_client
        self.public_space = public_space
        self.public_label = public_label

    def _to_joined_range(self, subnet_ranges: dict[str, list[dict]], label: str) -> str:
        """Convert a list of ip ranges to a string for cni config.

        Current cni config format is: <ip start>-<ip end>,<ip  start>-<ip end>,...
        """
        lb_range = []
        for ip_ranges in subnet_ranges.values():
            for ip_range in ip_ranges:
                if ip_range["label"] == label:
                    lb_range.append(f"{ip_range['start']}-{ip_range['end']}")
        if len(lb_range) == 0:
            raise ValueError("No ip range found for label: " + label)
        return ",".join(lb_range)

    def is_skip(self, status: Status | None = None):
        """Determines if the step should be skipped or not."""
        try:
            public_ranges = maas_client.get_ip_ranges_from_space(
                self.maas_client, self.public_space
            )
            LOG.debug("Public ip ranges: %r", public_ranges)
        except ValueError as e:
            LOG.debug(
                "Failed to ip ranges for space: %r", self.public_space, exc_info=True
            )
            return Result(ResultType.FAILED, str(e))
        try:
            public_metallb_range = self._to_joined_range(
                public_ranges, self.public_label
            )
        except ValueError:
            LOG.debug(
                "No iprange with label %r found",
                self.public_label,
                exc_info=True,
            )
            return Result(ResultType.FAILED, "No public ip range found")
        self.lb_range = public_metallb_range

        return self.check_k8s_status()


class MaasSetHypervisorUnitsOptionsStep(SetHypervisorUnitsOptionsStep):
    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        names: list[str],
        jhelper: JujuHelper,
        model: str,
        deployment_preseed: dict | None = None,
    ):
        super().__init__(
            client,
            names,
            jhelper,
            model,
            deployment_preseed or {},
            "Apply hypervisor settings",
            "Applying hypervisor settings",
        )
        self.maas_client = maas_client

    def _get_maas_nics(self) -> dict[str, str | None]:
        """Retrieve fist nic from MAAS per machine with compute tag.

        Return a dict of format:
            {
                "<machine>": "<nic1_name>" | None
            }
        """
        machines = maas_client.list_machines(self.maas_client, hostname=self.names)
        nics = {}
        for machine in machines:
            machine_nics = [
                nic["name"]
                for nic in machine["nics"]
                if maas_deployment.NicTags.COMPUTE.value in nic["tags"]
            ]

            if len(machine_nics) > 0:
                # take first nic with compute tag
                nic = machine_nics[0]
            else:
                nic = None
            nics[machine["hostname"]] = nic

        return nics

    def is_skip(self, status: Status | None = None):
        """Determines if the step should be skipped or not."""
        result = super().is_skip(status)
        if result.result_type == ResultType.FAILED:
            return result
        nics = self._get_maas_nics()
        LOG.debug("Nics: %r", nics)

        for machine, nic in nics.items():
            if nic is None:
                nic_tag = maas_deployment.NicTags.COMPUTE.value
                return Result(
                    ResultType.FAILED,
                    f"Machine {machine} does not have any {nic_tag} nic defined.",
                )

        self.nics = nics
        return Result(ResultType.COMPLETED)


class MaasUserQuestions(BaseStep):
    """Ask user configuration questions."""

    def __init__(
        self,
        client: Client,
        maas_client: maas_client.MaasClient,
        deployment_preseed: dict | None = None,
        accept_defaults: bool = False,
    ):
        super().__init__(
            "Collect cloud configuration", "Collecting cloud configuration"
        )
        self.client = client
        self.maas_client = maas_client
        self.accept_defaults = accept_defaults
        self.preseed = deployment_preseed or {}

    def has_prompts(self) -> bool:
        return True

    def prompt(self, console: Console | None = None) -> None:
        """Prompt the user for basic cloud configuration.

        Prompts the user for required information for cloud configuration.

        :param console: the console to prompt on
        :type console: rich.console.Console (Optional)
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        for section in ["user", "external_network"]:
            if not self.variables.get(section):
                self.variables[section] = {}

        user_bank = sunbeam.jobs.questions.QuestionBank(
            questions=maas_deployment.maas_user_questions(self.maas_client),
            console=console,
            preseed=self.preseed.get("user"),
            previous_answers=self.variables.get("user"),
            accept_defaults=self.accept_defaults,
        )
        self.variables["user"]["remote_access_location"] = sunbeam_utils.REMOTE_ACCESS
        # External Network Configuration
        ext_net_bank = sunbeam.jobs.questions.QuestionBank(
            questions=ext_net_questions(),
            console=console,
            preseed=self.preseed.get("external_network"),
            previous_answers=self.variables.get("external_network"),
            accept_defaults=self.accept_defaults,
        )
        self.variables["external_network"]["cidr"] = ext_net_bank.cidr.ask()
        external_network = ipaddress.ip_network(
            self.variables["external_network"]["cidr"]
        )
        external_network_hosts = list(external_network.hosts())
        default_gateway = self.variables["external_network"].get("gateway") or str(
            external_network_hosts[0]
        )
        self.variables["external_network"]["gateway"] = ext_net_bank.gateway.ask(
            new_default=default_gateway
        )

        default_allocation_range_start = self.variables["external_network"].get(
            "start"
        ) or str(external_network_hosts[1])
        self.variables["external_network"]["start"] = ext_net_bank.start.ask(
            new_default=default_allocation_range_start
        )
        default_allocation_range_end = self.variables["external_network"].get(
            "end"
        ) or str(external_network_hosts[-1])
        self.variables["external_network"]["end"] = ext_net_bank.end.ask(
            new_default=default_allocation_range_end
        )

        self.variables["external_network"]["physical_network"] = VARIABLE_DEFAULTS[
            "external_network"
        ]["physical_network"]

        self.variables["external_network"][
            "network_type"
        ] = ext_net_bank.network_type.ask()
        if self.variables["external_network"]["network_type"] == "vlan":
            self.variables["external_network"][
                "segmentation_id"
            ] = ext_net_bank.segmentation_id.ask()
        else:
            self.variables["external_network"]["segmentation_id"] = 0

        self.variables["user"]["run_demo_setup"] = user_bank.run_demo_setup.ask()
        if self.variables["user"]["run_demo_setup"]:
            # User configuration
            self.variables["user"]["username"] = user_bank.username.ask()
            self.variables["user"]["password"] = user_bank.password.ask()
            self.variables["user"]["cidr"] = user_bank.cidr.ask()
            self.variables["user"][
                "dns_nameservers"
            ] = user_bank.nameservers.ask().split()
            self.variables["user"][
                "security_group_rules"
            ] = user_bank.security_group_rules.ask()

        sunbeam.jobs.questions.write_answers(
            self.client, CLOUD_CONFIG_SECTION, self.variables
        )

    def run(self, status: Status | None = None) -> Result:
        return Result(ResultType.COMPLETED)
