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
from unittest.mock import AsyncMock, Mock, patch

import pytest

from sunbeam.commands.terraform import TerraformException
from sunbeam.jobs.common import ResultType
from sunbeam.jobs.juju import TimeoutException
from sunbeam.plugins.observability import plugin as observability_plugin


@pytest.fixture(autouse=True)
def mock_run_sync(mocker):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def run_sync(coro):
        return loop.run_until_complete(coro)

    mocker.patch("sunbeam.plugins.observability.plugin.run_sync", run_sync)
    yield
    loop.close()


@pytest.fixture()
def tfhelper():
    yield Mock()


@pytest.fixture()
def jhelper():
    yield AsyncMock()


@pytest.fixture()
def observabilityplugin():
    with patch("sunbeam.plugins.observability.plugin.ObservabilityPlugin") as p:
        yield p


@pytest.fixture()
def ssnap():
    with patch("sunbeam.commands.k8s.Snap") as p:
        yield p


class TestDeployObservabilityStackStep:
    def test_run(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        observabilityplugin.deployment.proxy_settings.return_value = {}
        step = observability_plugin.DeployObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        observabilityplugin.deployment.proxy_settings.return_value = {}
        tfhelper.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = observability_plugin.DeployObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        observabilityplugin.deployment.proxy_settings.return_value = {}
        jhelper.wait_until_active.side_effect = TimeoutException("timed out")

        step = observability_plugin.DeployObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_active.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestRemoveObservabilityStackStep:
    def test_run(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        step = observability_plugin.RemoveObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_model_gone.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_destroy_failed(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        tfhelper.destroy.side_effect = TerraformException("destroy failed...")

        step = observability_plugin.RemoveObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_model_gone.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "destroy failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, observabilityplugin, ssnap):
        ssnap().config.get.return_value = "k8s"
        jhelper.wait_model_gone.side_effect = TimeoutException("timed out")

        step = observability_plugin.RemoveObservabilityStackStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_model_gone.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestDeployGrafanaAgentStep:
    def test_run(self, tfhelper, jhelper, observabilityplugin):
        tfhelper_cos = Mock()
        step = observability_plugin.DeployGrafanaAgentStep(
            observabilityplugin, tfhelper, tfhelper_cos, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_application_ready.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, tfhelper, jhelper, observabilityplugin):
        tfhelper_cos = Mock()
        tfhelper.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = observability_plugin.DeployGrafanaAgentStep(
            observabilityplugin, tfhelper, tfhelper_cos, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_application_ready.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, observabilityplugin):
        tfhelper_cos = Mock()
        jhelper.wait_application_ready.side_effect = TimeoutException("timed out")

        step = observability_plugin.DeployGrafanaAgentStep(
            observabilityplugin, tfhelper, tfhelper_cos, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_application_ready.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestRemoveGrafanaAgentStep:
    def test_run(self, tfhelper, jhelper, observabilityplugin):
        step = observability_plugin.RemoveGrafanaAgentStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_destroy_failed(self, tfhelper, jhelper, observabilityplugin):
        tfhelper.destroy.side_effect = TerraformException("destroy failed...")

        step = observability_plugin.RemoveGrafanaAgentStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "destroy failed..."

    def test_run_waiting_timed_out(self, tfhelper, jhelper, observabilityplugin):
        jhelper.wait_application_gone.side_effect = TimeoutException("timed out")

        step = observability_plugin.RemoveGrafanaAgentStep(
            observabilityplugin, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestRemoveSaasApplicationsStep:
    def test_is_skip(self, jhelper):
        jhelper.get_model.return_value = AsyncMock(
            remote_applications={
                "test-1": Mock(offer_url="admin/offering_model.test-1")
            }
        )
        step = observability_plugin.RemoveSaasApplicationsStep(
            jhelper, "test", "offering_model"
        )
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_no_remote_app(self, jhelper):
        jhelper.get_model.return_value = AsyncMock(remote_applications={})
        step = observability_plugin.RemoveSaasApplicationsStep(
            jhelper, "test", "offering_model"
        )
        result = step.is_skip()
        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_no_saas_app(self, jhelper):
        jhelper.get_model.return_value = AsyncMock(
            remote_applications={
                "test-1": Mock(offer_url="admin/offering_model.test-1")
            }
        )
        step = observability_plugin.RemoveSaasApplicationsStep(
            jhelper, "test", "offering_model-no-apps"
        )
        result = step.is_skip()
        assert result.result_type == ResultType.SKIPPED

    def test_run(self, jhelper):
        step = observability_plugin.RemoveSaasApplicationsStep(
            jhelper, "test", "offering_model"
        )
        step._remote_app_to_delete = ["test-1"]
        result = step.run()
        assert result.result_type == ResultType.COMPLETED
