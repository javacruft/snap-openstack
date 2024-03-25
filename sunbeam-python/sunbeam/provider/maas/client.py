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

"""MAAS management."""

import collections
import logging
from typing import overload

from maas.client import bones, connect
from rich.console import Console

from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.deployments import DeploymentsConfig
from sunbeam.provider.maas.deployment import (
    MaasDeployment,
    Networks,
    RoleTags,
    StorageTags,
    is_maas_deployment,
)

LOG = logging.getLogger(__name__)
console = Console()


class MaasClient:
    """Facade to MAAS APIs."""

    def __init__(self, url: str, token: str, resource_pool: str | None = None):
        self._client = connect(url, apikey=token)
        self.resource_pool = resource_pool

    def get_resource_pool(self, name: str) -> object:
        """Fetch resource pool from MAAS."""
        return self._client.resource_pools.get(name)  # type: ignore

    def list_machines(self, **kwargs) -> list[dict]:
        """List machines."""
        if self.resource_pool:
            kwargs["pool"] = self.resource_pool
        try:
            return self._client.machines.list.__self__._handler.read(**kwargs)  # type: ignore # noqa
        except bones.CallError as e:
            if "No such pool" in str(e):
                raise ValueError(f"Resource pool {self.resource_pool!r} not found.")
            raise e

    def get_machine(self, machine: str) -> dict:
        """Get machine."""
        kwargs = {
            "hostname": machine,
        }
        if self.resource_pool:
            kwargs["pool"] = self.resource_pool
        machines = self._client.machines.list.__self__._handler.read(**kwargs)  # type: ignore # noqa
        if len(machines) == 0:
            raise ValueError(f"Machine {machine!r} not found.")
        if len(machines) > 1:
            raise ValueError(f"Machine {machine!r} not unique.")
        return machines[0]

    def list_spaces(self) -> list[dict]:
        """List spaces."""
        return self._client.spaces.list.__self__._handler.read()  # type: ignore

    def get_space(self, space: str) -> dict:
        """Get a specific space."""
        for space_raw in self.list_spaces():
            if space_raw["name"] == space:
                return space_raw
        else:
            raise ValueError(f"Space {space!r} not found.")

    def get_subnets(self, space: str | None = None) -> list[dict]:
        """List subnets."""
        if space:
            # check if space exists
            _ = self.get_space(space)
        subnets_response: list = self._client.subnets.list()  # type: ignore
        subnets = []
        for subnet in subnets_response:
            if space is None or subnet.space == space:
                subnets.append(subnet._data)
        return subnets

    def get_ip_ranges(self, subnet: dict) -> list[dict]:
        """List ip ranges.

        Only list reserved types as it is the only one we are interested in.
        """
        ip_ranges_response: list = self._client.ip_ranges.list()  # type: ignore

        subnet_id = subnet["id"]
        ip_ranges = []
        for ip_range in ip_ranges_response:
            if ip_range.subnet.id == subnet_id and ip_range.type.value == "reserved":
                ip_ranges.append(ip_range._data)
        return ip_ranges

    def get_dns_servers(self) -> list[str]:
        """Get configured upstream dns"""
        return self._client.maas.get_upstream_dns()  # type: ignore

    def get_http_proxy(self) -> str | None:
        """Get configured http proxy"""
        return self._client.maas.get_http_proxy()  # type: ignore

    @classmethod
    def from_deployment(cls, deployment: Deployment) -> "MaasClient":
        """Return client connected to active deployment."""
        if not is_maas_deployment(deployment):
            raise ValueError("Deployment is not a MAAS deployment.")
        return cls(
            deployment.url,
            deployment.token,
            deployment.resource_pool,
        )


def _to_root_disk(device: dict, partition: dict | None = None) -> dict:
    """Convert device to root disk."""
    if partition:
        size = partition["size"]
    else:
        size = device["size"]
    root_disk = {
        "name": device["name"],
        "tags": device["tags"],
        "root_partition": {
            "size": size,
        },
    }
    return root_disk


def _convert_raw_machine(machine_raw: dict) -> dict:
    storage_tags = StorageTags.values()
    storage_devices = {tag: [] for tag in storage_tags}
    root_disk = None
    for blockdevice in machine_raw["blockdevice_set"]:
        for tag in blockdevice["tags"]:
            if tag in storage_tags:
                storage_devices[tag].append(
                    {
                        "name": blockdevice["name"],
                        "id_path": blockdevice["id_path"],
                    }
                )
        if root_disk is not None:
            # root device already found, skipping
            continue
        if fs := blockdevice.get("filesystem"):
            if fs.get("label") == "root":
                root_disk = _to_root_disk(blockdevice)

        for partition in blockdevice.get("partitions", []):
            fs = partition.get("filesystem")
            if fs.get("label") == "root":
                root_disk = _to_root_disk(blockdevice, partition)

    spaces = []
    nics = []
    for interface in machine_raw["interface_set"]:
        if (vlan := interface.get("vlan")) and (space := vlan.get("space")):
            spaces.append(space)
        nics.append(
            {
                "id": interface["id"],
                "name": interface["name"],
                "mac_address": interface["mac_address"],
                "tags": interface["tags"],
            }
        )

    return {
        "system_id": machine_raw["system_id"],
        "hostname": machine_raw["hostname"],
        "roles": list(set(machine_raw["tag_names"]).intersection(RoleTags.values())),
        "zone": machine_raw["zone"]["name"],
        "status": machine_raw["status_name"],
        "root_disk": root_disk,
        "storage": storage_devices,
        "spaces": list(set(spaces)),
        "nics": nics,
        "cores": machine_raw["cpu_count"],
        "memory": machine_raw["memory"],
    }


def list_machines(client: MaasClient, **extra_args) -> list[dict]:
    """List machines in deployment, return consumable list of dicts."""
    machines_raw = client.list_machines(**extra_args)

    machines = []
    for machine in machines_raw:
        machines.append(_convert_raw_machine(machine))
    return machines


def get_machine(client: MaasClient, machine: str) -> dict:
    """Get machine in deployment, return consumable dict."""
    machine_raw = client.get_machine(machine)
    machine_dict = _convert_raw_machine(machine_raw)
    LOG.debug("Retrieved machine %s: %r", machine, machine_dict)
    return machine_dict


def _group_machines_by_zone(machines: list[dict]) -> dict[str, list[dict]]:
    """Helper to list machines by zone, return consumable dict."""
    result = collections.defaultdict(list)
    for machine in machines:
        result[machine["zone"]].append(machine)
    return dict(result)


def list_machines_by_zone(client: MaasClient) -> dict[str, list[dict]]:
    """List machines by zone, return consumable dict."""
    machines_raw = list_machines(client)
    return _group_machines_by_zone(machines_raw)


def list_spaces(client: MaasClient) -> list[dict]:
    """List spaces in deployment, return consumable list of dicts."""
    spaces_raw = client.list_spaces()
    spaces = []
    for space_raw in spaces_raw:
        space = {
            "name": space_raw["name"],
            "subnets": [subnet_raw["cidr"] for subnet_raw in space_raw["subnets"]],
        }
        spaces.append(space)
    return spaces


def map_space(
    deployments_config: DeploymentsConfig,
    deployment: MaasDeployment,
    client: MaasClient,
    space: str,
    network: Networks,
):
    """Map space to network."""
    space_raw = client.get_space(space)
    deployment.network_mapping[network.value] = space_raw["name"]
    deployments_config.update_deployment(deployment)
    deployments_config.write()


def unmap_space(
    deployments_config: DeploymentsConfig, deployment: MaasDeployment, network: Networks
):
    """Unmap network."""
    deployment.network_mapping.pop(network.value, None)
    deployments_config.update_deployment(deployment)
    deployments_config.write()


@overload
def get_network_mapping(deployment: MaasDeployment) -> dict[str, str | None]:
    pass


@overload
def get_network_mapping(deployment: DeploymentsConfig) -> dict[str, str | None]:
    pass


def get_network_mapping(
    deployment: MaasDeployment | DeploymentsConfig,
) -> dict[str, str | None]:
    """Return network mapping."""
    if isinstance(deployment, DeploymentsConfig):
        dep = deployment.get_active()
    else:
        dep = deployment
    if not is_maas_deployment(dep):
        raise ValueError(f"Deployment {dep.name} is not a MAAS deployment.")
    mapping = dep.network_mapping.copy()
    for network in Networks:
        mapping.setdefault(network.value, None)
    return mapping


def _convert_raw_ip_range(ip_range_raw: dict) -> dict:
    """Convert raw ip range to consumable dict."""
    return {
        "label": ip_range_raw["comment"],
        "start": ip_range_raw["start_ip"],
        "end": ip_range_raw["end_ip"],
    }


def get_ip_ranges_from_space(client: MaasClient, space: str) -> dict[str, list[dict]]:
    """Return all IP ranges from a space.

    Return a dict with the CIDR as key and a list of IP ranges as value.
    """
    subnets = client.get_subnets(space)
    ip_ranges = {}
    for subnet in subnets:
        ranges_raw = client.get_ip_ranges(subnet)
        ranges = []
        for ip_range in ranges_raw:
            ranges.append(_convert_raw_ip_range(ip_range))
        if len(ranges) > 0:
            ip_ranges[subnet["cidr"]] = ranges
    return ip_ranges
