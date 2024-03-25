# Copyright 2023 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from builtins import ConnectionRefusedError
from ssl import SSLError
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from maas.client.bones import CallError

import sunbeam.provider.maas.steps as maas_steps
from sunbeam.jobs.deployments import DeploymentsConfig
from sunbeam.jobs.juju import ControllerNotFoundException
from sunbeam.provider.maas.deployment import (
    MaasDeployment,
    Networks,
    NicTags,
    RoleTags,
    StorageTags,
)
from sunbeam.provider.maas.steps import (
    ActionFailedException,
    AddMaasDeployment,
    DeploymentRolesCheck,
    IpRangesCheck,
    MaasAddMachinesToClusterdStep,
    MaasBootstrapJujuStep,
    MaasConfigureMicrocephOSDStep,
    MaasDeployMachinesStep,
    MaasDeployMicrok8sApplicationStep,
    MaasScaleJujuStep,
    MachineComputeNicCheck,
    MachineNetworkCheck,
    MachineRequirementsCheck,
    MachineRolesCheck,
    MachineRootDiskCheck,
    MachineStorageCheck,
    Result,
    ResultType,
    UnitNotFoundException,
    ZoneBalanceCheck,
    ZonesCheck,
)


class TestAddMaasDeployment:
    @pytest.fixture
    def add_maas_deployment(self):
        return AddMaasDeployment(
            Mock(),
            MaasDeployment(
                name="test_deployment",
                token="test_token",
                url="test_url",
                resource_pool="test_resource_pool",
            ),
        )

    def test_is_skip_with_existing_deployment(self, add_maas_deployment):
        deployments_config = DeploymentsConfig(
            active="test_deployment",
            deployments=[
                MaasDeployment(
                    name="test_deployment",
                    url="test_url2",
                    resource_pool="test_resource_pool3",
                    token="test_token",
                )
            ],
        )
        add_maas_deployment.deployments_config = deployments_config
        result = add_maas_deployment.is_skip()
        assert result.result_type == ResultType.FAILED

    def test_is_skip_with_existing_url_and_resource_pool(self, add_maas_deployment):
        deployments_config = DeploymentsConfig(
            active="test_deployment",
            deployments=[
                MaasDeployment(
                    name="different_deployment",
                    url="test_url",
                    resource_pool="test_resource_pool",
                    token="test_token",
                )
            ],
        )
        add_maas_deployment.deployments_config = deployments_config
        result = add_maas_deployment.is_skip()
        assert result.result_type == ResultType.FAILED

    def test_is_skip_with_no_existing_deployment(self, add_maas_deployment):
        deployments_config = DeploymentsConfig()
        add_maas_deployment.deployments_config = deployments_config
        result = add_maas_deployment.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_successful_connection(self, add_maas_deployment, mocker):
        mocker.patch("sunbeam.provider.maas.client.MaasClient", autospec=True)
        result = add_maas_deployment.run()
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_connection_refused_error(self, add_maas_deployment, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.MaasClient",
            side_effect=ConnectionRefusedError("Connection refused"),
        )
        result = add_maas_deployment.run()
        assert result.result_type == ResultType.FAILED

    def test_run_with_ssl_error(self, add_maas_deployment, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.MaasClient", side_effect=SSLError("SSL error")
        )
        result = add_maas_deployment.run()
        assert result.result_type == ResultType.FAILED

    def test_run_with_call_error(self, add_maas_deployment, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.MaasClient",
            side_effect=CallError(
                request=dict(method="GET", uri="http://localhost:5240/MAAS"),
                response=Mock(status=401, reason="unauthorized"),
                content=b"",
                call=None,
            ),
        )
        result = add_maas_deployment.run()
        assert result.result_type == ResultType.FAILED

    def test_run_with_unknown_error(self, add_maas_deployment, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.MaasClient",
            side_effect=Exception("Unknown error"),
        )
        result = add_maas_deployment.run()
        assert result.result_type == ResultType.FAILED


class TestMachineRolesCheck:
    def test_run_with_no_assigned_roles(self):
        machine = {"hostname": "test_machine", "roles": []}
        check = MachineRolesCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"

    def test_run_with_assigned_roles(self):
        machine = {"hostname": "test_machine", "roles": ["role1", "role2"]}
        check = MachineRolesCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"


class TestMachineNetworkCheck:
    def test_run_with_incomplete_network_mapping(self, mocker):
        snap = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.get_network_mapping", return_value={}
        )
        machine = {
            "hostname": "test_machine",
            "roles": ["role1", "role2"],
            "spaces": [],
        }
        check = MachineNetworkCheck(snap, machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "network mapping" in result.message

    def test_run_with_no_assigned_roles(self, mocker):
        snap = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.get_network_mapping",
            return_value={network: "alpha" for network in Networks.values()},
        )
        machine = {"hostname": "test_machine", "roles": [], "spaces": []}
        check = MachineNetworkCheck(snap, machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "no role assigned" in result.message

    def test_run_with_missing_spaces(self, mocker):
        snap = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.get_network_mapping",
            return_value={
                **{network.value: "alpha" for network in Networks},
                **{Networks.PUBLIC.value: "beta"},
            },
        )
        machine = {
            "hostname": "test_machine",
            "roles": [RoleTags.CONTROL.value],
            "spaces": ["alpha"],
        }
        check = MachineNetworkCheck(snap, machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "missing beta" in result.message

    def test_run_with_successful_check(self, mocker):
        snap = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.get_network_mapping",
            return_value={network.value: "alpha" for network in Networks},
        )
        machine = {
            "hostname": "test_machine",
            "roles": RoleTags.values(),
            "spaces": ["alpha"],
        }
        check = MachineNetworkCheck(snap, machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"


class TestMachineStorageCheck:
    def test_run_with_no_assigned_roles(self):
        machine = {"hostname": "test_machine", "roles": [], "storage": {}}
        check = MachineStorageCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "machine has no role assigned" in result.message

    def test_run_with_not_storage_node(self):
        machine = {
            "hostname": "test_machine",
            "roles": ["role1", "role2"],
            "storage": {},
        }
        check = MachineStorageCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"
        assert result.message == "not a storage node."

    def test_run_with_no_ceph_storage(self):
        machine = {
            "hostname": "test_machine",
            "roles": [RoleTags.STORAGE.value],
            "storage": {},
        }
        check = MachineStorageCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "storage node has no ceph storage" in result.message
        assert result.diagnostics
        assert "https://maas.io/docs/using-storage-tags" in result.diagnostics

    def test_run_with_ceph_storage(self):
        machine = {
            "hostname": "test_machine",
            "roles": [RoleTags.STORAGE.value],
            "storage": {StorageTags.CEPH.value: ["/disk_a"]},
        }
        check = MachineStorageCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"
        assert result.message and StorageTags.CEPH.value in result.message


class TestMachineComputeNicCheck:
    def test_run_with_no_assigned_roles(self):
        machine = {"hostname": "test_machine", "roles": [], "nics": []}
        check = MachineComputeNicCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "machine has no role assigned" in result.message

    def test_run_with_not_compute_node(self):
        machine = {
            "hostname": "test_machine",
            "roles": ["role1", "role2"],
            "nics": [],
        }
        check = MachineComputeNicCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"
        assert result.message == "not a compute node."

    def test_run_with_no_compute_nic(self):
        machine = {
            "hostname": "test_machine",
            "roles": [RoleTags.COMPUTE.value],
            "nics": [],
        }
        check = MachineComputeNicCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "no compute nic found" in result.message
        assert result.diagnostics
        assert "https://maas.io/docs/using-network-tags" in result.diagnostics

    def test_run_with_compute_nic(self):
        machine = {
            "hostname": "test_machine",
            "roles": [RoleTags.COMPUTE.value],
            "nics": [{"name": "eth0", "tags": [NicTags.COMPUTE.value]}],
        }
        check = MachineComputeNicCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"
        assert result.message and NicTags.COMPUTE.value in result.message


class TestMachineRootDiskCheck:
    def test_run_with_no_root_disk(self):
        machine = {"hostname": "test_machine"}
        check = MachineRootDiskCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "could not determine" in result.message

    def test_run_with_no_ssd_tag(self):
        machine = {"hostname": "test_machine", "root_disk": {"tags": []}}
        check = MachineRootDiskCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "is not a SSD" in result.message

    def test_run_with_not_enough_space(self):
        machine = {
            "hostname": "test_machine",
            "root_disk": {"tags": ["ssd"], "root_partition": {"size": 1}},
        }
        check = MachineRootDiskCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "is too small" in result.message

    def test_run_with_valid_root_disk(self):
        machine = {
            "hostname": "test_machine",
            "root_disk": {"tags": ["ssd"], "root_partition": {"size": 500 * 1024**3}},
        }
        check = MachineRootDiskCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"
        assert result.message and "is a SSD and is large enough" in result.message


class TestMachineRequirementsCheck:
    def test_run_with_insufficient_memory(self):
        machine = {
            "hostname": "test_machine",
            "cores": 16,
            "memory": 16384,  # 16GiB
            "roles": [RoleTags.CONTROL.value],
        }
        check = MachineRequirementsCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "machine does not meet requirements" in result.message

    def test_run_with_insufficient_cores(self):
        machine = {
            "hostname": "test_machine",
            "cores": 8,
            "memory": 32768,  # 32GiB
            "roles": [RoleTags.CONTROL.value],
        }
        check = MachineRequirementsCheck(machine)
        result = check.run()
        assert result.passed is False
        assert result.details["machine"] == "test_machine"
        assert result.message and "machine does not meet requirements" in result.message

    def test_run_with_sufficient_resources(self):
        machine = {
            "hostname": "test_machine",
            "cores": 16,
            "memory": 32768,  # 32GB
            "roles": [RoleTags.CONTROL.value],
        }
        check = MachineRequirementsCheck(machine)
        result = check.run()
        assert result.passed is True
        assert result.details["machine"] == "test_machine"


class TestDeploymentRolesCheck:
    def test_run_with_insufficient_roles(self):
        machines = [
            {"hostname": "machine1", "roles": ["role1", "role2"]},
            {"hostname": "machine2", "roles": ["role1"]},
            {"hostname": "machine3", "roles": ["role2"]},
        ]
        check = DeploymentRolesCheck(machines, "Role", "role1", min_count=3)
        result = check.run()
        assert result.passed is False
        assert result.message and "less than 3 Role" in result.message

    def test_run_with_sufficient_roles(self):
        machines = [
            {"hostname": "machine1", "roles": ["role1", "role2"]},
            {"hostname": "machine2", "roles": ["role1"]},
            {"hostname": "machine3", "roles": ["role1"]},
        ]
        check = DeploymentRolesCheck(machines, "Role", "role1", min_count=3)
        result = check.run()
        assert result.passed is True
        assert result.message == "Role: 3"


class TestZonesCheck:
    def test_run_with_one_zone(self):
        zones = ["zone1"]
        check = ZonesCheck(zones)
        result = check.run()
        assert result.passed is True
        assert result.message == "1 zone(s)"

    def test_run_with_two_zones(self):
        zones = ["zone1", "zone2"]
        check = ZonesCheck(zones)
        result = check.run()
        assert result.passed is False
        assert result.message == "deployment has 2 zones"

    def test_run_with_three_zones(self):
        zones = ["zone1", "zone2", "zone3"]
        check = ZonesCheck(zones)
        result = check.run()
        assert result.passed is True
        assert result.message == "3 zone(s)"


class TestZoneBalanceCheck:
    def test_run_with_balanced_roles(self):
        machines = {
            "zone1": [
                {"roles": [RoleTags.CONTROL.value, RoleTags.STORAGE.value]},
                {"roles": [RoleTags.CONTROL.value, RoleTags.COMPUTE.value]},
            ],
            "zone2": [
                {"roles": [RoleTags.CONTROL.value, RoleTags.STORAGE.value]},
                {"roles": [RoleTags.CONTROL.value, RoleTags.COMPUTE.value]},
            ],
        }
        check = ZoneBalanceCheck(machines)
        result = check.run()
        assert result.passed is True
        assert result.message == "deployment is balanced"

    def test_run_with_unbalanced_roles(self):
        machines = {
            "zone1": [
                {"roles": [RoleTags.CONTROL.value, RoleTags.STORAGE.value]},
                {"roles": [RoleTags.CONTROL.value, RoleTags.COMPUTE.value]},
            ],
            "zone2": [
                {"roles": [RoleTags.CONTROL.value, RoleTags.STORAGE.value]},
                {"roles": [RoleTags.CONTROL.value]},
            ],
        }
        check = ZoneBalanceCheck(machines)
        result = check.run()
        assert result.passed is False
        assert result.message and "compute distribution is unbalanced" in result.message


class TestIpRangesCheck:
    def test_run_with_missing_network_mapping(self, mocker):
        client = Mock()
        deployment = Mock()
        deployment.network_mapping = {}
        check = IpRangesCheck(client, deployment)
        result = check.run()
        assert result.passed is False
        assert result.diagnostics and "network mapping" in result.diagnostics

    def test_run_with_missing_public_ip_ranges(self, mocker):
        client = Mock()
        deployment = Mock()
        deployment.network_mapping = {
            **{
                network.value: "data"
                for network in Networks
                if network != Networks.PUBLIC
            },
            **{Networks.PUBLIC.value: "public_space"},
        }
        deployment.public_api_label = "public_api"
        get_ip_ranges_from_space_mock = mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space", return_value={}
        )
        check = IpRangesCheck(client, deployment)
        result = check.run()
        assert result.passed is False
        assert result.diagnostics and deployment.public_api_label in result.diagnostics
        get_ip_ranges_from_space_mock.assert_any_call(client, "public_space")

    def test_run_with_missing_internal_ip_ranges(self, mocker):
        client = Mock()
        deployment = Mock()
        deployment.network_mapping = {
            **{
                network.value: "data"
                for network in Networks
                if network != Networks.INTERNAL
            },
            **{Networks.INTERNAL.value: "internal_space"},
        }
        deployment.public_api_label = "public_api"
        deployment.internal_api_label = "internal_api"

        public_ip_ranges = {
            "any_cidr": [
                {
                    "start": "192.168.0.1",
                    "end": "192.168.0.10",
                    "label": "public_api",
                },
            ]
        }

        get_ip_ranges_from_space_mock = mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            side_effect=[public_ip_ranges, {}],
        )
        check = IpRangesCheck(client, deployment)
        result = check.run()
        assert result.passed is False
        assert (
            result.diagnostics and deployment.internal_api_label in result.diagnostics
        )
        get_ip_ranges_from_space_mock.assert_any_call(client, "internal_space")

    def test_run_with_successful_check(self, mocker):
        client = Mock()
        deployment = Mock()
        deployment.network_mapping = {
            Networks.PUBLIC.value: "public_space",
            Networks.INTERNAL.value: "internal_space",
            **{
                network.value: "data"
                for network in Networks
                if network not in (Networks.PUBLIC, Networks.INTERNAL)
            },
        }
        deployment.public_api_label = "public_api"
        deployment.internal_api_label = "internal_api"

        public_ip_ranges = {
            "192.168.0.0/24": [
                {
                    "start": "192.168.0.1",
                    "end": "192.168.0.10",
                    "label": "public_api",
                }
            ]
        }
        internal_ip_ranges = {
            "10.0.0.0/24": [
                {
                    "start": "10.0.0.1",
                    "end": "10.0.0.10",
                    "label": "internal_api",
                }
            ]
        }

        get_ip_ranges_from_space_mock = mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            side_effect=[public_ip_ranges, internal_ip_ranges],
        )
        check = IpRangesCheck(client, deployment)
        result = check.run()
        assert result.passed is True
        get_ip_ranges_from_space_mock.assert_any_call(client, "public_space")
        get_ip_ranges_from_space_mock.assert_any_call(client, "internal_space")


class TestMaasBootstrapJujuStep:
    def test_is_skip_with_no_machines(self, snap, mocker):
        maas_client = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.list_machines",
            return_value=[],
        )
        mocker.patch.object(maas_steps, "Snap", return_value=snap)
        step = MaasBootstrapJujuStep(
            maas_client=maas_client,
            cloud="test_cloud",
            cloud_type="test_cloud_type",
            controller="test_controller",
            password="test_password",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message and "No machines with tag" in result.message

    def test_is_skip_with_multiple_machines(self, snap, mocker):
        maas_client = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.list_machines",
            return_value=[
                {"hostname": "machine1", "system_id": "1st"},
                {"hostname": "machine2", "system_id": "2nd"},
            ],
        )
        mocker.patch(
            "sunbeam.commands.juju.BootstrapJujuStep.is_skip",
            return_value=Result(ResultType.COMPLETED),
        )
        mocker.patch.object(maas_steps, "Snap", return_value=snap)
        step = MaasBootstrapJujuStep(
            maas_client=maas_client,
            cloud="test_cloud",
            cloud_type="test_cloud_type",
            controller="test_controller",
            password="test_password",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED
        assert "--to" in step.bootstrap_args
        assert step.bootstrap_args[-1].endswith("1st")

    def test_is_skip_with_single_machine(self, snap, mocker):
        maas_client = Mock()
        mocker.patch(
            "sunbeam.provider.maas.client.list_machines",
            return_value=[
                {"hostname": "machine1", "system_id": "1st"},
            ],
        )
        mocker.patch(
            "sunbeam.commands.juju.BootstrapJujuStep.is_skip",
            return_value=Result(ResultType.COMPLETED),
        )
        mocker.patch.object(maas_steps, "Snap", return_value=snap)
        step = MaasBootstrapJujuStep(
            maas_client=maas_client,
            cloud="test_cloud",
            cloud_type="test_cloud_type",
            controller="test_controller",
            password="test_password",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED
        assert "--to" in step.bootstrap_args
        assert step.bootstrap_args[-1].endswith("1st")


class TestMaasScaleJujuStep:
    def test_is_skip_with_controller_not_found(self, mocker):
        maas_client = mocker.Mock()
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        mocker.patch.object(
            step,
            "get_controller",
            side_effect=ControllerNotFoundException("Controller not found"),
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == f"Controller {controller} not found"

    def test_is_skip_with_no_registered_machines(self, mocker):
        maas_client = mocker.Mock()
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        mocker.patch.object(
            step, "get_controller", return_value={"controller-machines": None}
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == f"Controller {controller} has no machines registered."

    def _controller_machines_raw(self) -> list[dict]:
        return [
            {
                "hostname": "c-1",
                "blockdevice_set": [],
                "interface_set": [],
                "zone": {"name": "default"},
                "tag_names": [RoleTags.JUJU_CONTROLLER.value],
                "status_name": "deployed",
                "cpu_count": 4,
                "memory": 32768,
            },
            {
                "hostname": "c-1",
                "blockdevice_set": [],
                "interface_set": [],
                "zone": {"name": "default"},
                "tag_names": [RoleTags.JUJU_CONTROLLER.value],
                "status_name": "deployed",
                "cpu_count": 4,
                "memory": 32768,
            },
        ]

    def test_is_skip_with_already_correct_number_of_controllers(self, mocker):
        maas_client = mocker.Mock(
            list_machines=Mock(return_value=self._controller_machines_raw())
        )
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        step.n = 2
        mocker.patch.object(
            step, "get_controller", return_value={"controller-machines": [1, 2]}
        )
        result = step.is_skip()
        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_with_cannot_scale_down_controllers(self, mocker):
        maas_client = mocker.Mock(
            list_machines=Mock(return_value=self._controller_machines_raw())
        )
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        step.n = 1
        mocker.patch.object(
            step, "get_controller", return_value={"controller-machines": [1, 2]}
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == f"Can't scale down controllers from 2 to {step.n}."

    def test_is_skip_with_insufficient_juju_controllers(self, mocker):
        maas_client = mocker.Mock()
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        step.n = 3
        mocker.patch.object(
            step, "get_controller", return_value={"controller-machines": [1, 2]}
        )
        mocker.patch("sunbeam.provider.maas.client.list_machines", return_value=[1, 2])
        result = step.is_skip()
        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_with_completed(self, mocker):
        maas_client = mocker.Mock()
        controller = "test_controller"
        step = MaasScaleJujuStep(maas_client, controller)
        step.n = 3
        mocker.patch.object(
            step,
            "get_controller",
            return_value={"controller-machines": {"1": {"instance-id": "1st"}}},
        )
        mocker.patch(
            "sunbeam.provider.maas.client.list_machines",
            return_value=[
                {"hostname": "machine1", "system_id": "1st"},
                {"hostname": "machine2", "system_id": "2nd"},
                {"hostname": "machine3", "system_id": "3rd"},
            ],
        )
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED


class TestMaasAddMachinesToClusterdStep:
    @pytest.fixture
    def maas_add_machines_to_clusterd_step(self):
        client = Mock()
        maas_client = Mock()
        return MaasAddMachinesToClusterdStep(client, maas_client)

    def test_is_skip_with_no_filtered_machines(
        self, mocker, maas_add_machines_to_clusterd_step
    ):
        mocker.patch("sunbeam.provider.maas.client.list_machines", return_value=[])
        result = maas_add_machines_to_clusterd_step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Maas deployment has no machines."

    def test_is_skip_with_filtered_machines(
        self, mocker, maas_add_machines_to_clusterd_step
    ):
        mocker.patch(
            "sunbeam.provider.maas.client.list_machines",
            return_value=[
                {"hostname": "machine1", "roles": [RoleTags.CONTROL.value]},
                {"hostname": "machine2", "roles": [RoleTags.COMPUTE.value]},
            ],
        )
        maas_add_machines_to_clusterd_step.client.cluster.list_nodes.return_value = []
        result = maas_add_machines_to_clusterd_step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_no_machines_and_nodes(self, maas_add_machines_to_clusterd_step):
        maas_add_machines_to_clusterd_step.machines = None
        maas_add_machines_to_clusterd_step.nodes = None
        result = maas_add_machines_to_clusterd_step.run()
        assert result.result_type == ResultType.FAILED
        assert result.message == "No machines to add / node to update."

    def test_run_with_machines_and_nodes(self, maas_add_machines_to_clusterd_step):
        maas_add_machines_to_clusterd_step.machines = [
            {
                "hostname": "machine1",
                "roles": [RoleTags.CONTROL.value],
                "system_id": "1st",
            },
            {
                "hostname": "machine2",
                "roles": [RoleTags.COMPUTE.value],
                "system_id": "2nd",
            },
        ]
        maas_add_machines_to_clusterd_step.nodes = [
            ("machine1", [RoleTags.CONTROL.value]),
            ("machine2", [RoleTags.COMPUTE.value]),
        ]
        result = maas_add_machines_to_clusterd_step.run()
        assert result.result_type == ResultType.COMPLETED


class TestMaasDeployMachinesStep:
    @pytest.fixture
    def maas_deploy_machines_step(self):
        client = Mock()
        jhelper = AsyncMock()
        model = "test_model"
        return MaasDeployMachinesStep(client, jhelper, model)

    def test_is_skip_with_no_clusterd_nodes(self, maas_deploy_machines_step):
        maas_deploy_machines_step.client.cluster.list_nodes.return_value = []
        result = maas_deploy_machines_step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "No machines to deploy."

    def test_is_skip_with_existing_machine_id(self, maas_deploy_machines_step):
        maas_deploy_machines_step.client.cluster.list_nodes.return_value = [
            {"name": "test_node", "machineid": 1}
        ]
        maas_deploy_machines_step.jhelper.get_machines.return_value = {
            "2": Mock(hostname="test_node")
        }
        result = maas_deploy_machines_step.is_skip()
        assert result.result_type == ResultType.FAILED
        msg = (
            "Machine test_node already exists in model test_model with id 2,"
            " expected the id 1."
        )
        assert result.message == msg

    def test_is_skip_with_nodes_to_deploy(self, maas_deploy_machines_step):
        maas_deploy_machines_step.client.cluster.list_nodes.return_value = [
            {"name": "test_node", "machineid": -1}
        ]
        maas_deploy_machines_step.jhelper.get_machines.return_value = {}
        result = maas_deploy_machines_step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_run(self, maas_deploy_machines_step):
        maas_deploy_machines_step.nodes_to_deploy = [
            {"name": "test_node1", "systemid": "1st"},
            {"name": "test_node2", "systemid": "2nd"},
        ]
        maas_deploy_machines_step.nodes_to_update = [
            {"name": "test_node3"},
            {"name": "test_node4"},
        ]
        maas_deploy_machines_step.jhelper.get_model.return_value = Mock(
            machines={
                "1": Mock(hostname="test_node3", id=1),
                "2": Mock(hostname="test_node4", id=2),
            }
        )
        result = maas_deploy_machines_step.run()
        assert result.result_type == ResultType.COMPLETED
        assert maas_deploy_machines_step.client.cluster.update_node_info.call_count == 4
        assert (
            maas_deploy_machines_step.jhelper.wait_all_machines_deployed.call_count == 1
        )


class TestMaasConfigureMicrocephOSDStep:
    @pytest.fixture
    def jhelper(self):
        jhelper = Mock()
        jhelper.get_leader_unit = AsyncMock(return_value="leader_unit")
        jhelper.get_unit_from_machine = AsyncMock(return_value="unit/1")
        return jhelper

    @pytest.fixture
    def step(self, jhelper):
        client = Mock()
        maas_client = Mock()
        names = ["machine1", "machine2"]
        step = MaasConfigureMicrocephOSDStep(
            client, maas_client, jhelper, names, "test-model"
        )
        return step

    @pytest.fixture
    def microceph_disks(self):
        return {
            "machine1": {
                "osds": ["/dev/sdb", "/dev/sdc"],
                "unpartitioned_disks": ["/dev/sdd"],
                "unit": "unit/1",
            },
            "machine2": {
                "osds": ["/dev/sde"],
                "unpartitioned_disks": ["/dev/sdf", "/dev/sdg"],
                "unit": "unit/2",
            },
        }

    @pytest.fixture
    def maas_disks(self):
        return {
            "machine1": ["/dev/sdb", "/dev/sdc"],
            "machine2": ["/dev/sde", "/dev/sdf"],
        }

    @pytest.fixture
    def step_with_disks(self, step, microceph_disks, maas_disks):
        step._get_microceph_disks = AsyncMock(return_value=microceph_disks)
        step._get_maas_disks = Mock(return_value=maas_disks)
        return step

    @pytest.mark.asyncio
    async def test_get_microceph_disks(self, step, jhelper, microceph_disks):
        osds = (
            '[{"location": "machine1", "path": "/dev/sdb"},'
            ' {"location": "machine1", "path": "/dev/sdc"},'
            ' {"location": "machine2", "path": "/dev/sde"}]'
        )
        leader_result = {
            "osds": osds,
            "unpartitioned-disks": '[{"path": "/dev/sdd"}]',
        }
        jhelper.run_action = AsyncMock(
            side_effect=[
                leader_result,
                leader_result,
                {
                    "osds": (osds),
                    "unpartitioned-disks": '[{"path": "/dev/sdf"},'
                    ' {"path": "/dev/sdg"}]',
                },
            ]
        )
        step.client.cluster.get_node_info.return_value = {"machineid": 1}
        step.client.cluster.list_nodes.return_value = [
            {"name": "machine1"},
            {"name": "machine2"},
        ]
        step.jhelper.get_unit_from_machine.side_effect = [
            Mock(entity_id="unit/1"),
            Mock(entity_id="unit/2"),
        ]
        step.jhelper.get_machines.return_value = {
            "machine1": Mock(hostname="test_node1"),
            "machine2": Mock(hostname="test_node2"),
        }
        step.jhelper.get_model.return_value = Mock(
            machines={
                "1": Mock(hostname="test_node1", id=1),
                "2": Mock(hostname="test_node2", id=2),
            }
        )

        # Call the method under test
        result = await step._get_microceph_disks()

        # Assert the result
        assert result == microceph_disks

    @pytest.mark.asyncio
    async def test_list_disks(self, step, jhelper):
        jhelper.run_action = AsyncMock(
            return_value={
                "osds": (
                    '[{"location": "machine1", "path": "/dev/sdb"},'
                    ' {"location": "machine1", "path": "/dev/sdc"}]'
                ),
                "unpartitioned-disks": '[{"path": "/dev/sdd"}]',
            }
        )
        result = await step._list_disks("unit1")
        assert result == (
            [
                {"location": "machine1", "path": "/dev/sdb"},
                {"location": "machine1", "path": "/dev/sdc"},
            ],
            [{"path": "/dev/sdd"}],
        )

    def test_compute_disks_to_configure(self, step):
        microceph_disks = {
            "osds": ["/dev/sdb", "/dev/sdc"],
            "unpartitioned_disks": ["/dev/sdd", "/dev/sde"],
            "unit": "unit/1",
        }
        maas_disks = {"/dev/sdb", "/dev/sdc", "/dev/sdd"}
        result = step._compute_disks_to_configure(microceph_disks, maas_disks)
        assert result == ["/dev/sdd"]

    def test_compute_disks_to_configure_no_maas_disks(self, step):
        microceph_disks = {
            "osds": ["/dev/sdb", "/dev/sdc"],
            "unpartitioned_disks": ["/dev/sdd"],
            "unit": "unit/1",
        }
        maas_disks = set()
        with pytest.raises(ValueError) as e:
            step._compute_disks_to_configure(microceph_disks, maas_disks)
        assert str(e.value) == "Machine 'unit/1' does not have any 'ceph' disk defined."

    def test_compute_disks_to_configure_unknown_osds(self, step):
        microceph_disks = {
            "osds": ["/dev/sdb", "/dev/sdc", "/dev/sdd"],
            "unpartitioned_disks": ["/dev/sde"],
            "unit": "unit/1",
        }
        maas_disks = {"/dev/sdb", "/dev/sdc"}
        with pytest.raises(ValueError) as e:
            step._compute_disks_to_configure(microceph_disks, maas_disks)
        exc_msg = "Machine 'unit/1' has OSDs from disks unknown to MAAS: {'/dev/sdd'}"
        assert str(e.value) == exc_msg

    def test_compute_disks_to_configure_missing_disks(self, step):
        microceph_disks = {
            "osds": ["/dev/sdb", "/dev/sdc"],
            "unpartitioned_disks": ["/dev/sdd"],
            "unit": "unit1",
        }
        maas_disks = {"/dev/sdb", "/dev/sdc", "/dev/sde"}
        with pytest.raises(ValueError) as e:
            step._compute_disks_to_configure(microceph_disks, maas_disks)
        assert str(e.value) == "Machine 'unit1' is missing disks: {'/dev/sde'}"

    def test_is_skip_completed(self, step_with_disks):
        result = step_with_disks.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_failed_get_microceph_disks(self, step):
        step._get_microceph_disks = AsyncMock(
            side_effect=ValueError("Failed to list microceph disks from units")
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Failed to list microceph disks from units"

    def test_is_skip_failed_get_maas_disks(self, step):
        step._get_microceph_disks = AsyncMock(return_value={})
        step._get_maas_disks = MagicMock(
            side_effect=ValueError("Failed to list disks from MAAS")
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Failed to list disks from MAAS"

    def test_run(self, step_with_disks, jhelper):
        jhelper.run_action = AsyncMock(return_value={"status": "completed"})
        result = step_with_disks.run()
        assert result.result_type == ResultType.COMPLETED

    def test_run_failed_run_action(self, step_with_disks, jhelper):
        step_with_disks.disks_to_configure = {"unit/1": ["/dev/sdd"]}
        jhelper.run_action = AsyncMock(
            side_effect=ActionFailedException("Failed to run action")
        )
        result = step_with_disks.run()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Failed to run action"

    def test_run_failed_unit_not_found(self, step_with_disks, jhelper):
        step_with_disks.disks_to_configure = {"unit/1": ["/dev/sdd"]}
        jhelper.run_action = AsyncMock(
            side_effect=UnitNotFoundException("Unit not found")
        )
        result = step_with_disks.run()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Unit not found"


class TestMaasDeployMicrok8sApplicationStep:
    def test_extra_tfvars_with_ranges_none(self):
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        with pytest.raises(ValueError):
            step.extra_tfvars()

    def test_extra_tfvars_with_ranges(self):
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        step.ranges = "10.0.0.1-10.0.0.10,10.0.0.20-10.0.0.30"
        expected_tfvars = {
            "addons": {
                "dns": "",
                "hostpath-storage": "",
                "metallb": "10.0.0.1-10.0.0.10,10.0.0.20-10.0.0.30",
            }
        }
        assert step.extra_tfvars() == expected_tfvars

    def test_is_skip_with_public_ranges_error(self, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            side_effect=ValueError("Failed to get ip ranges"),
        )
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Failed to get ip ranges"

    def test_is_skip_with_no_public_ranges(self, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            return_value={},
        )
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "No public ip range found"

    def test_is_skip_with_internal_ranges_error(self, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            side_effect=[
                {
                    "10.0.0.0/24": [
                        {
                            "start": "10.0.0.10",
                            "end": "10.0.0.20",
                            "label": "public_api",
                        }
                    ]
                },
                ValueError("Failed to get ip ranges"),
            ],
        )
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "Failed to get ip ranges"

    def test_is_skip_with_no_internal_ranges(self, mocker):
        mocker.patch(
            "sunbeam.provider.maas.client.get_ip_ranges_from_space",
            side_effect=[
                {
                    "10.0.0.0/24": [
                        {
                            "start": "10.0.0.10",
                            "end": "10.0.0.20",
                            "label": "public_api",
                        }
                    ]
                },
                {},
            ],
        )
        step = MaasDeployMicrok8sApplicationStep(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "public_space",
            "public_api",
            "internal_space",
            "internal_api",
            "test-model",
        )
        result = step.is_skip()
        assert result.result_type == ResultType.FAILED
        assert result.message == "No internal ip range found"
