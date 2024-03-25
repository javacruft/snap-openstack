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

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

import sunbeam.plugins.interface.v1.openstack as openstack
from sunbeam.commands.terraform import TerraformException
from sunbeam.jobs.common import ResultType
from sunbeam.jobs.juju import TimeoutException
from sunbeam.jobs.manifest import Manifest


@pytest.fixture(autouse=True)
def mock_run_sync(mocker):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def run_sync(coro):
        return loop.run_until_complete(coro)

    mocker.patch("sunbeam.plugins.interface.v1.openstack.run_sync", run_sync)
    yield
    loop.close()


@pytest.fixture()
def cclient():
    yield Mock()


@pytest.fixture()
def read_config():
    with patch("sunbeam.plugins.interface.v1.openstack.read_config") as p:
        p.return_value = {}
        yield p


@pytest.fixture()
def jhelper():
    yield AsyncMock()


@pytest.fixture()
def tfhelper():
    yield Mock(path=Path())


@pytest.fixture()
def osplugin():
    with patch(
        "sunbeam.plugins.interface.v1.openstack.OpenStackControlPlanePlugin"
    ) as p:
        yield p


@pytest.fixture()
def manifest():
    with patch.object(Manifest, "load_latest_from_clusterdb_on_default") as p:
        yield p


@pytest.fixture()
def pluginmanager():
    with patch("sunbeam.jobs.manifest.PluginManager") as p:
        yield p


class TestEnableOpenStackApplicationStep:
    def test_run(
        self,
        cclient,
        jhelper,
        osplugin,
    ):
        step = openstack.EnableOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(
        self, cclient, read_config, jhelper, tfhelper, osplugin, manifest, pluginmanager
    ):
        osplugin.manifest.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = openstack.EnableOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(
        self, cclient, read_config, jhelper, tfhelper, osplugin, manifest, pluginmanager
    ):
        jhelper.wait_until_active.side_effect = TimeoutException("timed out")

        step = openstack.EnableOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestDisableOpenStackApplicationStep:
    def test_run(self, cclient, jhelper, osplugin):
        step = openstack.DisableOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, cclient, jhelper, osplugin):
        osplugin.manifest.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = openstack.DisableOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."


class MockStatus:
    def __init__(self, value: dict):
        self.status = value

    def to_json(self):
        return json.dumps(self.status)


class TestUpgradeOpenStackApplicationStep:
    def test_run(
        self,
        cclient,
        jhelper,
        osplugin,
    ):
        jhelper.get_model_status_full.return_value = MockStatus(
            {
                "applications": {
                    "keystone": {
                        "charm": "ch:amd64/jammy/keystone-k8s-148",
                        "charm-channel": "2023.2/stable",
                    }
                }
            }
        )
        step = openstack.UpgradeOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(
        self, cclient, read_config, jhelper, tfhelper, osplugin, manifest, pluginmanager
    ):
        osplugin.manifest.update_partial_tfvars_and_apply_tf.side_effect = (
            TerraformException("apply failed...")
        )

        jhelper.get_model_status_full.return_value = MockStatus(
            {
                "applications": {
                    "keystone": {
                        "charm": "ch:amd64/jammy/keystone-k8s-148",
                        "charm-channel": "2023.2/stable",
                    }
                }
            }
        )
        step = openstack.UpgradeOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(
        self, cclient, read_config, jhelper, tfhelper, osplugin, manifest, pluginmanager
    ):
        jhelper.wait_until_desired_status.side_effect = TimeoutException("timed out")

        jhelper.get_model_status_full.return_value = MockStatus(
            {
                "applications": {
                    "keystone": {
                        "charm": "ch:amd64/jammy/keystone-k8s-148",
                        "charm-channel": "2023.2/stable",
                    }
                }
            }
        )
        step = openstack.UpgradeOpenStackApplicationStep(jhelper, osplugin)
        result = step.run()

        osplugin.manifest.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"
