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
import unittest
from unittest.mock import AsyncMock, Mock

import pytest

from sunbeam.commands.terraform import TerraformException
from sunbeam.jobs.common import ResultType
from sunbeam.jobs.juju import TimeoutException
from sunbeam.plugins.pro.plugin import (
    DisableUbuntuProApplicationStep,
    EnableUbuntuProApplicationStep,
)


@pytest.fixture(autouse=True)
def mock_run_sync(mocker):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def run_sync(coro):
        return loop.run_until_complete(coro)

    mocker.patch("sunbeam.plugins.pro.plugin.run_sync", run_sync)
    yield
    loop.close()


class TestEnableUbuntuProApplicationStep(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.jhelper = AsyncMock()
        self.manifest = Mock()
        self.tfplan = "fake-plan"
        self.model = "test-model"
        self.token = "TOKENFORTESTING"

    def test_is_skip(self):
        step = EnableUbuntuProApplicationStep(
            self.client,
            self.manifest,
            self.jhelper,
            self.token,
            self.tfplan,
            self.model,
        )
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_has_prompts(self):
        step = EnableUbuntuProApplicationStep(
            self.client,
            self.manifest,
            self.jhelper,
            self.token,
            self.tfplan,
            self.model,
        )
        assert not step.has_prompts()

    def test_enable(self):
        step = EnableUbuntuProApplicationStep(
            self.client,
            self.manifest,
            self.jhelper,
            self.token,
            self.tfplan,
            self.model,
        )
        result = step.run()
        self.manifest.update_tfvars_and_apply_tf.assert_called_with(
            self.client,
            tfplan=self.tfplan,
            tfvar_config=None,
            override_tfvars={"token": self.token},
        )
        self.jhelper.wait_application_ready.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_enable_tf_apply_failed(self):
        self.manifest.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = EnableUbuntuProApplicationStep(
            self.client,
            self.manifest,
            self.jhelper,
            self.token,
            self.tfplan,
            self.model,
        )
        result = step.run()

        self.manifest.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_enable_waiting_timed_out(self):
        self.jhelper.wait_application_ready.side_effect = TimeoutException("timed out")

        step = EnableUbuntuProApplicationStep(
            self.client,
            self.manifest,
            self.jhelper,
            self.token,
            self.tfplan,
            self.model,
        )
        result = step.run()

        self.jhelper.wait_application_ready.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestDisableUbuntuProApplicationStep(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.jhelper = AsyncMock()
        self.manifest = Mock()
        self.tfplan = "fake-plan"

    def test_is_skip(self):
        step = DisableUbuntuProApplicationStep(self.client, self.manifest, self.tfplan)
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    def test_has_prompts(self):
        step = DisableUbuntuProApplicationStep(self.client, self.manifest, self.tfplan)
        assert not step.has_prompts()

    def test_disable(self):
        step = DisableUbuntuProApplicationStep(self.client, self.manifest, self.tfplan)
        result = step.run()
        self.manifest.update_tfvars_and_apply_tf.assert_called_with(
            self.client,
            tfplan=self.tfplan,
            tfvar_config=None,
            override_tfvars={"token": ""},
        )
        assert result.result_type == ResultType.COMPLETED

    def test_disable_tf_apply_failed(self):
        self.manifest.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )

        step = DisableUbuntuProApplicationStep(self.client, self.manifest, self.tfplan)
        result = step.run()

        self.manifest.update_tfvars_and_apply_tf.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."
