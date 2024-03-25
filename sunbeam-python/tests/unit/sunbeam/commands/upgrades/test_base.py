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

from unittest.mock import AsyncMock, Mock

from sunbeam.commands.terraform import TerraformException
from sunbeam.commands.upgrades.inter_channel import BaseUpgrade
from sunbeam.jobs.common import ResultType
from sunbeam.jobs.juju import TimeoutException


class TestBaseUpgrade:
    def setup_method(self):
        self.client = Mock()
        self.jhelper = AsyncMock()
        self.manifest = Mock()

    def test_upgrade_applications(self):
        model = "openstack"
        apps = ["nova"]
        charms = ["nova-k8s"]
        tfplan = "openstack-plan"
        config = "openstackterraformvar"
        timeout = 60

        upgrader = BaseUpgrade(
            "test name",
            "test description",
            self.client,
            self.jhelper,
            self.manifest,
            model,
        )

        result = upgrader.upgrade_applications(
            apps, charms, model, tfplan, config, timeout
        )
        self.manifest.update_partial_tfvars_and_apply_tf.assert_called_once_with(
            self.client, charms, tfplan, config
        )
        self.jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_upgrade_applications_tf_failed(self):
        self.manifest.update_partial_tfvars_and_apply_tf.side_effect = (
            TerraformException("apply failed...")
        )

        model = "openstack"
        apps = ["nova"]
        charms = ["nova-k8s"]
        tfplan = "openstack-plan"
        config = "openstackterraformvar"
        timeout = 60

        upgrader = BaseUpgrade(
            "test name",
            "test description",
            self.client,
            self.jhelper,
            self.manifest,
            model,
        )

        result = upgrader.upgrade_applications(
            apps, charms, model, tfplan, config, timeout
        )
        self.manifest.update_partial_tfvars_and_apply_tf.assert_called_once_with(
            self.client, charms, tfplan, config
        )
        self.jhelper.wait_until_desired_status.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_upgrade_applications_waiting_timed_out(self):
        self.jhelper.wait_until_desired_status.side_effect = TimeoutException(
            "timed out"
        )

        model = "openstack"
        apps = ["nova"]
        charms = ["nova-k8s"]
        tfplan = "openstack-plan"
        config = "openstackterraformvar"
        timeout = 60

        upgrader = BaseUpgrade(
            "test name",
            "test description",
            self.client,
            self.jhelper,
            self.manifest,
            model,
        )

        result = upgrader.upgrade_applications(
            apps, charms, model, tfplan, config, timeout
        )
        self.manifest.update_partial_tfvars_and_apply_tf.assert_called_once_with(
            self.client, charms, tfplan, config
        )
        self.jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"
