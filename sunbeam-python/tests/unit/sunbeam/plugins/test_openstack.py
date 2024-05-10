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
    yield Mock()


class TestEnableOpenStackApplicationStep:
    def test_run(self, tfhelper, jhelper, osplugin):
        step = openstack.EnableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, jhelper, tfhelper, osplugin):
        tfhelper.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = openstack.EnableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, jhelper, tfhelper, osplugin):
        jhelper.wait_until_active.side_effect = TimeoutException("timed out")

        step = openstack.EnableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestDisableOpenStackApplicationStep:
    def test_run(self, tfhelper, jhelper, osplugin):
        step = openstack.DisableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, tfhelper, jhelper, osplugin):
        tfhelper.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = openstack.DisableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, osplugin):
        jhelper.wait_application_gone.side_effect = TimeoutException("timed out")

        step = openstack.DisableOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_application_gone.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class MockStatus:
    def __init__(self, value: dict):
        self.status = value

    def to_json(self):
        return json.dumps(self.status)


class TestUpgradeOpenStackApplicationStep:
    def test_run(
        self,
        tfhelper,
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
        step = openstack.UpgradeOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, tfhelper, jhelper, osplugin):
        tfhelper.update_partial_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
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
        step = openstack.UpgradeOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, osplugin):
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
        step = openstack.UpgradeOpenStackApplicationStep(tfhelper, jhelper, osplugin)
        result = step.run()

        tfhelper.update_partial_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"
