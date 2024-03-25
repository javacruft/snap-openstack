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
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pexpect
import pytest

import sunbeam.commands.juju as juju
from sunbeam.jobs.common import ResultType
from sunbeam.jobs.juju import ModelNotFoundException


@pytest.fixture(autouse=True)
def mock_run_sync(mocker):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def run_sync(coro):
        return loop.run_until_complete(coro)

    mocker.patch("sunbeam.commands.juju.run_sync", run_sync)
    yield
    loop.close()


@pytest.fixture()
def jhelper():
    yield AsyncMock()


@pytest.fixture()
def mock_open():
    with patch.object(Path, "open") as p:
        yield p


class TestJujuStepHelper:
    def test_revision_update_needed(self, jhelper):
        jsh = juju.JujuStepHelper()
        jsh.jhelper = jhelper
        CHARMREV = {"nova-k8s": 31, "cinder-k8s": 51, "another-k8s": 70}

        def _get_available_charm_revision(model, charm_name, deployed_channel):
            return CHARMREV[charm_name]

        _status = {
            "applications": {
                "nova": {
                    "charm": "ch:amd64/jammy/nova-k8s-30",
                    "charm-channel": "2023.2/edge/gnuoy",
                },
                "cinder": {
                    "charm": "ch:amd64/jammy/cinder-k8s-50",
                    "charm-channel": "2023.2/edge",
                },
                "another": {
                    "charm": "ch:amd64/jammy/another-k8s-70",
                    "charm-channel": "edge",
                },
            }
        }
        jhelper.get_available_charm_revision.side_effect = _get_available_charm_revision
        assert jsh.revision_update_needed("cinder", "openstack", _status)
        assert not jsh.revision_update_needed("nova", "openstack", _status)
        assert not jsh.revision_update_needed("another", "openstack", _status)

    def test_normalise_channel(self):
        jsh = juju.JujuStepHelper()
        assert jsh.normalise_channel("2023.2/edge") == "2023.2/edge"
        assert jsh.normalise_channel("edge") == "latest/edge"

    def test_extract_charm_name(self):
        jsh = juju.JujuStepHelper()
        assert jsh._extract_charm_name("ch:amd64/jammy/cinder-k8s-50") == "cinder-k8s"

    def test_extract_charm_revision(self):
        jsh = juju.JujuStepHelper()
        assert jsh._extract_charm_revision("ch:amd64/jammy/cinder-k8s-50") == "50"

    def test_channel_update_needed(self):
        jsh = juju.JujuStepHelper()
        assert jsh.channel_update_needed("2023.1/stable", "2023.2/stable")
        assert jsh.channel_update_needed("2023.1/stable", "2023.1/edge")
        assert jsh.channel_update_needed("latest/stable", "latest/edge")
        assert not jsh.channel_update_needed("2023.1/stable", "2023.1/stable")
        assert not jsh.channel_update_needed("2023.2/stable", "2023.1/stable")
        assert not jsh.channel_update_needed("latest/stable", "latest/stable")
        assert not jsh.channel_update_needed("foo/stable", "ba/stable")


class TestWriteJujuStatusStep:
    def test_is_skip(self, jhelper):
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteJujuStatusStep(jhelper, "openstack", tmpfile)
            result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_when_model_not_present(self, jhelper):
        jhelper.get_model.side_effect = ModelNotFoundException("not found")
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteJujuStatusStep(jhelper, "openstack", tmpfile)
            result = step.is_skip()

        assert result.result_type == ResultType.SKIPPED

    def test_run(self, jhelper):
        status_mock = Mock()
        status_mock.to_json.return_value = (
            '{"applications": {"controller": {"status": "active"}}}'
        )
        jhelper.get_model_status_full.return_value = status_mock
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteJujuStatusStep(jhelper, "openstack", Path(tmpfile.name))
            result = step.run()

        jhelper.get_model_status_full.assert_called_once()
        assert result.result_type == ResultType.COMPLETED


class TestWriteCharmLogStep:
    def test_is_skip(self, jhelper):
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteCharmLogStep(jhelper, "openstack", tmpfile)
            result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_when_model_not_present(self, jhelper):
        jhelper.get_model.side_effect = ModelNotFoundException("not found")
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteCharmLogStep(jhelper, "openstack", tmpfile)
            result = step.is_skip()

        assert result.result_type == ResultType.SKIPPED

    def test_run(self, mocker, snap, check_call, mock_open):
        mocker.patch.object(juju, "Snap", return_value=snap)
        with tempfile.NamedTemporaryFile() as tmpfile:
            step = juju.WriteCharmLogStep(jhelper, "openstack", Path(tmpfile.name))
            result = step.run()

        assert result.result_type == ResultType.COMPLETED


class TestJujuGrantModelAccessStep:
    def test_run(self, mocker, snap, jhelper, run):
        mocker.patch.object(juju, "Snap", return_value=snap)
        jhelper.get_model_name_with_owner.return_value = "admin/control-plane"
        step = juju.JujuGrantModelAccessStep(jhelper, "fakeuser", "control-plane")
        result = step.run()

        jhelper.get_model_name_with_owner.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_model_not_exist(self, mocker, snap, jhelper, run):
        mocker.patch.object(juju, "Snap", return_value=snap)
        jhelper.get_model_name_with_owner.side_effect = ModelNotFoundException(
            "Model 'missing' not found"
        )
        step = juju.JujuGrantModelAccessStep(jhelper, "fakeuser", "missing")
        result = step.run()

        jhelper.get_model_name_with_owner.assert_called_once()
        run.assert_not_called()
        assert result.result_type == ResultType.FAILED


class TestJujuLoginStep:
    def test_is_skip_when_juju_account_not_present(self):
        step = juju.JujuLoginStep(None)
        assert step.is_skip().result_type == ResultType.SKIPPED

    def test_run(self):
        with patch(
            "sunbeam.commands.juju.pexpect.spawn",
            Mock(
                return_value=Mock(
                    __enter__=Mock(return_value=Mock(exitstatus=0)), __exit__=Mock()
                )
            ),
        ):
            step = juju.JujuLoginStep(Mock(user="test", password="test"))
            step._get_juju_binary = Mock(return_value="juju")
            assert step.is_skip().result_type == ResultType.COMPLETED

        with patch(
            "sunbeam.commands.juju.pexpect.spawn", Mock(return_value=Mock(exitstatus=0))
        ):
            result = step.run()
        assert result.result_type == ResultType.COMPLETED

    def test_run_pexpect_timeout(self):
        with patch(
            "sunbeam.commands.juju.pexpect.spawn",
            Mock(
                return_value=Mock(
                    __enter__=Mock(return_value=Mock(exitstatus=0)), __exit__=Mock()
                )
            ),
        ):
            step = juju.JujuLoginStep(Mock(user="test", password="test"))
            step._get_juju_binary = Mock(return_value="juju")
            assert step.is_skip().result_type == ResultType.COMPLETED

        with patch(
            "sunbeam.commands.juju.pexpect.spawn",
            Mock(
                return_value=Mock(
                    exitstatus=0, expect=Mock(side_effect=pexpect.TIMEOUT("timeout"))
                )
            ),
        ):
            result = step.run()
        assert result.result_type == ResultType.FAILED

    def test_run_pexpect_failed_exitcode(self):
        with patch(
            "sunbeam.commands.juju.pexpect.spawn",
            Mock(
                return_value=Mock(
                    __enter__=Mock(return_value=Mock(exitstatus=0)), __exit__=Mock()
                )
            ),
        ):
            step = juju.JujuLoginStep(Mock(user="test", password="test"))
            step._get_juju_binary = Mock(return_value="juju")
            assert step.is_skip().result_type == ResultType.COMPLETED

        with patch(
            "sunbeam.commands.juju.pexpect.spawn", Mock(return_value=Mock(exitstatus=1))
        ):
            result = step.run()
        assert result.result_type == ResultType.FAILED


class TestAddCloudJujuStep:
    def test_is_skip(self):
        cloud_name = "my-cloud"
        cloud_definition = {
            "clouds": {
                cloud_name: {
                    "type": "my-cloud-type",
                    # Add other required fields for the cloud definition
                }
            }
        }
        step = juju.AddCloudJujuStep(cloud_name, cloud_definition)

        with patch.object(step, "get_clouds") as mock_get_clouds:
            mock_get_clouds.return_value = [cloud_name]
            result = step.is_skip()

        mock_get_clouds.assert_called_once_with("my-cloud-type", local=True)
        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_when_exception_raised(self):
        cloud_name = "my-cloud"
        cloud_definition = {
            "clouds": {
                "my-cloud": {
                    "type": "my-cloud-type",
                    # Add other required fields for the cloud definition
                }
            }
        }
        step = juju.AddCloudJujuStep(cloud_name, cloud_definition)

        with patch.object(step, "get_clouds") as mock_get_clouds:
            mock_get_clouds.side_effect = subprocess.CalledProcessError(
                cmd="juju clouds", returncode=1, output="Error output"
            )

            result = step.is_skip()

        mock_get_clouds.assert_called_once_with("my-cloud-type", local=True)
        assert result.result_type == ResultType.FAILED

    def test_run(self):
        cloud_name = "my-cloud"
        cloud_definition = {
            "clouds": {
                "my-cloud": {
                    "type": "my-cloud-type",
                    # Add other required fields for the cloud definition
                }
            }
        }
        step = juju.AddCloudJujuStep(cloud_name, cloud_definition)

        with patch.object(step, "add_cloud") as mock_add_cloud:
            mock_add_cloud.return_value = True

            result = step.run()

        mock_add_cloud.assert_called_once_with("my-cloud", cloud_definition)
        assert result.result_type == ResultType.COMPLETED

    def test_run_when_exception_raised(self):
        cloud_name = "my-cloud"
        cloud_definition = {
            "clouds": {
                "my-cloud": {
                    "type": "my-cloud-type",
                    # Add other required fields for the cloud definition
                }
            }
        }
        step = juju.AddCloudJujuStep(cloud_name, cloud_definition)

        with patch.object(step, "add_cloud") as mock_add_cloud:
            mock_add_cloud.side_effect = subprocess.CalledProcessError(
                cmd="juju add-cloud", returncode=1, output="Error output"
            )

            result = step.run()

        mock_add_cloud.assert_called_once_with("my-cloud", cloud_definition)
        assert result.result_type == ResultType.FAILED


class TestAddCredentialsJujuStep:
    def test_is_skip(self, mocker):
        cloud = "my-cloud"
        credentials = "my-credentials"
        definition = {"key": "value"}

        step = juju.AddCredentialsJujuStep(cloud, credentials, definition)

        mocker.patch.object(step, "get_credentials", return_value={})
        result = step.is_skip()

        step.get_credentials.assert_called_once_with(cloud, local=True)
        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_when_credentials_exist(self, mocker):
        cloud = "my-cloud"
        credentials = "my-credentials"
        definition = {"key": "value"}

        step = juju.AddCredentialsJujuStep(cloud, credentials, definition)

        mocker.patch.object(
            step,
            "get_credentials",
            return_value={
                "client-credentials": {
                    cloud: {"cloud-credentials": {credentials: {"key": "value"}}}
                }
            },
        )
        result = step.is_skip()

        step.get_credentials.assert_called_once_with(cloud, local=True)
        assert result.result_type == ResultType.SKIPPED

    def test_run(self, mocker):
        cloud = "my-cloud"
        credentials = "my-credentials"
        definition = {"key": "value"}

        step = juju.AddCredentialsJujuStep(cloud, credentials, definition)

        mocker.patch.object(step, "add_credential")
        result = step.run()

        step.add_credential.assert_called_once_with(cloud, definition)
        assert result.result_type == ResultType.COMPLETED

    def test_run_failed(self, mocker):
        cloud = "my-cloud"
        credentials = "my-credentials"
        definition = {"key": "value"}

        step = juju.AddCredentialsJujuStep(cloud, credentials, definition)

        mocker.patch.object(
            step,
            "add_credential",
            side_effect=subprocess.CalledProcessError(1, "command"),
        )
        result = step.run()

        step.add_credential.assert_called_once_with(cloud, definition)
        assert result.result_type == ResultType.FAILED


class TestScaleJujuStep:
    def test_is_skip(self):
        step = juju.ScaleJujuStep("controller", n=3, extra_args=["--arg1", "--arg2"])
        result = step.is_skip()
        assert result.result_type == ResultType.COMPLETED

    @patch("subprocess.run")
    def test_run(self, mock_run, mocker):
        step = juju.ScaleJujuStep("controller", n=3, extra_args=["--arg1", "--arg2"])
        mocker.patch.object(step, "_get_juju_binary", return_value="/juju-mock")
        result = step.run()

        assert mock_run.call_count == 2
        assert result.result_type == ResultType.COMPLETED
