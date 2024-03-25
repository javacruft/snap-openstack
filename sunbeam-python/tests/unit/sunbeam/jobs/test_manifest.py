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

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml
from pydantic import ValidationError

import sunbeam.commands.terraform as terraform
import sunbeam.jobs.manifest as manifest
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ManifestItemNotFoundException,
)
from sunbeam.jobs.common import ResultType
from sunbeam.versions import OPENSTACK_CHANNEL, TERRAFORM_DIR_NAMES

test_manifest = """
software:
  juju:
    bootstrap_args:
      - --agent-version=3.2.4
  charms:
    keystone-k8s:
      channel: 2023.1/stable
      revision: 234
      config:
        debug: True
    glance-k8s:
      channel: 2023.1/stable
      revision: 134
  terraform:
    openstack-plan:
      source: /home/ubuntu/openstack-tf
    hypervisor-plan:
      source: /home/ubuntu/hypervisor-tf
"""

malformed_test_manifest = """
software:
  charms:
    keystone-k8s:
      channel: 2023.1/stable
      revision: 234
      conf
"""

test_manifest_invalid_values = """
software:
  charms:
    keystone-k8s:
      channel: 2023.1/stable
      revision: 234
      # Config value should be dictionary but provided str
      config: debug
"""

test_manifest_incorrect_terraform_key = """
software:
  charms:
    keystone-k8s:
      channel: 2023.1/stable
      revision: 234
      config:
        debug: True
  terraform:
    fake-plan:
      source: /home/ubuntu/tfplan
"""


@pytest.fixture()
def deployment():
    mock = Mock()
    mock.name = "test_deployment"
    mock.juju_controller.api_endpoints = ["ep1", "ep2"]
    yield mock


@pytest.fixture()
def pluginmanager():
    with patch("sunbeam.jobs.manifest.PluginManager") as p:
        yield p


@pytest.fixture()
def tfhelper():
    with patch("sunbeam.jobs.manifest.TerraformHelper") as p:
        yield p


@pytest.fixture()
def read_config():
    with patch("sunbeam.jobs.manifest.read_config") as p:
        yield p


@pytest.fixture()
def update_config():
    with patch("sunbeam.jobs.manifest.update_config") as p:
        yield p


class TestManifest:
    def test_load(self, mocker, snap, deployment, pluginmanager, tmpdir):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        manifest_obj = manifest.Manifest.load(deployment, manifest_file)
        ks_manifest = manifest_obj.software_config.charms.get("keystone-k8s")
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Assert defaults does not exist
        assert "nova" not in manifest_obj.software_config.charms.keys()

        test_manifest_dict = yaml.safe_load(test_manifest)
        assert (
            manifest_obj.software_config.juju.bootstrap_args
            == test_manifest_dict.get("software", {})  # noqa W503
            .get("juju", {})
            .get("bootstrap_args", [])
        )

    def test_load_on_default(self, mocker, snap, deployment, pluginmanager, tmpdir):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        manifest_obj = manifest.Manifest.load(
            deployment, manifest_file, include_defaults=True
        )

        # Check updates from manifest file
        ks_manifest = manifest_obj.software_config.charms.get("keystone-k8s")
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Check default ones
        nova_manifest = manifest_obj.software_config.charms.get("nova-k8s")
        assert nova_manifest.channel == OPENSTACK_CHANNEL
        assert nova_manifest.revision is None
        assert nova_manifest.config is None

    def test_load_latest_from_clusterdb(self, mocker, snap, deployment, pluginmanager):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(deployment)
        ks_manifest = manifest_obj.software_config.charms.get("keystone-k8s")
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Assert defaults does not exist
        assert "nova-k8s" not in manifest_obj.software_config.charms.keys()

    def test_load_latest_from_clusterdb_on_default(
        self, mocker, snap, deployment, pluginmanager
    ):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=True
        )
        ks_manifest = manifest_obj.software_config.charms.get("keystone-k8s")
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Check default ones
        nova_manifest = manifest_obj.software_config.charms.get("nova-k8s")
        assert nova_manifest.channel == OPENSTACK_CHANNEL
        assert nova_manifest.revision is None
        assert nova_manifest.config is None

    def test_get_default_manifest(self, mocker, snap, deployment, pluginmanager):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        default_manifest = manifest.Manifest.get_default_manifest(deployment)
        nova_manifest = default_manifest.software_config.charms.get("nova-k8s")
        assert nova_manifest.channel == OPENSTACK_CHANNEL
        assert nova_manifest.revision is None
        assert nova_manifest.config is None

    def test_malformed_manifest(self, mocker, snap, deployment, pluginmanager, tmpdir):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(malformed_test_manifest)
        with pytest.raises(yaml.scanner.ScannerError):
            manifest.Manifest.load(deployment, manifest_file)

    def test_load_manifest_invalid_values(
        self, mocker, snap, deployment, pluginmanager, tmpdir
    ):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest_invalid_values)
        with pytest.raises(ValidationError):
            manifest.Manifest.load(deployment, manifest_file)

    def test_validate_terraform_keys(
        self, mocker, snap, deployment, pluginmanager, tmpdir
    ):
        mocker.patch.object(manifest, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest_incorrect_terraform_key)
        with pytest.raises(ValueError):
            manifest.Manifest.load(deployment, manifest_file)

    def test_get_tfhelper(self, mocker, snap, copytree, deployment, pluginmanager):
        tfplan = "microk8s-plan"
        mocker.patch.object(manifest, "Snap", return_value=snap)
        mocker.patch.object(terraform, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=True
        )
        tfhelper = manifest_obj.get_tfhelper(tfplan)
        tfplan_dir = TERRAFORM_DIR_NAMES.get(tfplan)
        copytree.assert_called_once_with(
            Path(snap.paths.snap / "etc" / tfplan_dir),
            Path(snap.paths.user_common / "etc" / deployment.name / tfplan_dir),
            dirs_exist_ok=True,
        )
        assert tfhelper.plan == tfplan

    def test_get_tfhelper_tfplan_override_in_manifest(
        self, mocker, snap, copytree, deployment, pluginmanager
    ):
        tfplan = "openstack-plan"
        mocker.patch.object(manifest, "Snap", return_value=snap)
        mocker.patch.object(terraform, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=True
        )
        tfhelper = manifest_obj.get_tfhelper(tfplan)
        tfplan_dir = TERRAFORM_DIR_NAMES.get(tfplan)
        test_manifest_dict = yaml.safe_load(test_manifest)
        copytree.assert_called_once_with(
            Path(
                test_manifest_dict.get("software", "")
                .get("terraform", {})
                .get("openstack-plan", {})
                .get("source")
            ),
            Path(snap.paths.user_common / "etc" / deployment.name / tfplan_dir),
            dirs_exist_ok=True,
        )
        assert tfhelper.plan == tfplan

    def test_get_tfhelper_multiple_calls(
        self, mocker, snap, copytree, deployment, pluginmanager
    ):
        tfplan = "openstack-plan"
        mocker.patch.object(manifest, "Snap", return_value=snap)
        mocker.patch.object(terraform, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=True
        )
        manifest_obj.get_tfhelper(tfplan)
        assert copytree.call_count == 1
        # Calling second time should return the value from cache instead of creating
        # new object
        manifest_obj.get_tfhelper(tfplan)
        assert copytree.call_count == 1

    def test_get_tfhelper_missing_terraform_source(
        self, mocker, snap, copytree, deployment, pluginmanager
    ):
        tfplan = "microk8s-plan"
        mocker.patch.object(manifest, "Snap", return_value=snap)
        mocker.patch.object(terraform, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=False
        )
        with pytest.raises(manifest.MissingTerraformInfoException):
            manifest_obj.get_tfhelper(tfplan)
        copytree.assert_not_called()

    def test_update_tfvars_and_apply_tf(
        self,
        mocker,
        snap,
        copytree,
        deployment,
        pluginmanager,
        tfhelper,
        read_config,
        update_config,
    ):
        tfplan = "openstack-plan"
        extra_tfvars = {
            "ldap-apps": {"dom2": {"domain-name": "dom2"}},
            "glance-revision": 555,
        }
        read_config.return_value = {
            "keystone-channel": OPENSTACK_CHANNEL,
            "neutron-channel": "2023.1/stable",
            "neutron-revision": 123,
            "ldap-apps": {"dom1": {"domain-name": "dom1"}},
        }
        mocker.patch.object(manifest, "Snap", return_value=snap)
        mocker.patch.object(terraform, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest_obj = manifest.Manifest.load_latest_from_clusterdb(
            deployment, include_defaults=True
        )
        manifest_obj.update_tfvars_and_apply_tf(
            client, tfplan, "fake-config", extra_tfvars
        )
        manifest_obj.tf_helpers.get(tfplan).write_tfvars.assert_called_once()
        manifest_obj.tf_helpers.get(tfplan).apply.assert_called_once()
        applied_tfvars = manifest_obj.tf_helpers.get(
            tfplan
        ).write_tfvars.call_args.args[0]

        # Assert values coming from manifest and not in config db
        assert applied_tfvars.get("glance-channel") == "2023.1/stable"

        # Assert values coming from manifest and in config db
        assert applied_tfvars.get("keystone-channel") == "2023.1/stable"
        assert applied_tfvars.get("keystone-revision") == 234
        assert applied_tfvars.get("keystone-config") == {"debug": True}

        # Assert values coming from default not in config db
        assert applied_tfvars.get("nova-channel") == OPENSTACK_CHANNEL

        # Assert values coming from default and in config db
        assert applied_tfvars.get("neutron-channel") == OPENSTACK_CHANNEL

        # Assert values coming from extra_tfvars and in config db
        assert applied_tfvars.get("ldap-apps") == extra_tfvars.get("ldap-apps")

        # Assert values coming from extra_tfvars and in manifest
        assert applied_tfvars.get("glance-revision") == 555

        # Assert remove keys from read_config if not present in manifest+defaults
        # or override
        assert "neutron-revision" not in applied_tfvars.keys()


class TestAddManifestStep:
    def test_is_skip(self, tmpdir):
        # Manifest in cluster DB different from user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": "charms: {}"}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest.AddManifestStep(client, manifest_file)
        result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_apply_same_manifest(self, tmpdir):
        # Manifest in cluster DB same as user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest.AddManifestStep(client, manifest_file)
        result = step.is_skip()

        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_no_manifest(self):
        # Manifest in cluster DB same as user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        step = manifest.AddManifestStep(client)
        result = step.is_skip()

        assert step.manifest_content == manifest.EMPTY_MANIFEST
        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_no_manifest_apply_same(self):
        # Manifest in cluster DB same as user provided manifest
        empty_manifest_str = yaml.safe_dump(manifest.EMPTY_MANIFEST)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": empty_manifest_str}
        step = manifest.AddManifestStep(client)
        result = step.is_skip()

        assert step.manifest_content == manifest.EMPTY_MANIFEST
        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_no_connection_to_clusterdb(self):
        client = Mock()
        client.cluster.get_latest_manifest.side_effect = (
            ClusterServiceUnavailableException("Cluster unavailable..")
        )
        step = manifest.AddManifestStep(client)
        result = step.is_skip()

        assert result.result_type == ResultType.FAILED

    def test_is_skip_with_no_manifest_in_db(self):
        client = Mock()
        client.cluster.get_latest_manifest.side_effect = ManifestItemNotFoundException(
            "Manifest Item not found."
        )
        step = manifest.AddManifestStep(client)
        result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_run(self, tmpdir):
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": "charms: {}"}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest.AddManifestStep(client, manifest_file)
        step.manifest_content = yaml.safe_load(test_manifest)
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(step.manifest_content)
        )
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_no_manifest(self):
        client = Mock()
        step = manifest.AddManifestStep(client)
        step.manifest_content = manifest.EMPTY_MANIFEST
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(manifest.EMPTY_MANIFEST)
        )
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_no_connection_to_clusterdb(self):
        client = Mock()
        client.cluster.add_manifest.side_effect = ClusterServiceUnavailableException(
            "Cluster unavailable.."
        )
        step = manifest.AddManifestStep(client)
        step.manifest_content = manifest.EMPTY_MANIFEST
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(manifest.EMPTY_MANIFEST)
        )
        assert result.result_type == ResultType.FAILED
