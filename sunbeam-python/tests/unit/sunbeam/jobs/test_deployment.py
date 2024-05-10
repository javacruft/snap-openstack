# Copyright 2024 Canonical Ltd.
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
import functools
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest
import yaml

import sunbeam.commands.terraform as terraform_mod
import sunbeam.jobs.deployment as deployment_mod
import sunbeam.jobs.manifest as manifest_mod
from sunbeam.jobs.deployment import Deployment
from sunbeam.versions import (
    MANIFEST_CHARM_VERSIONS,
    OPENSTACK_CHANNEL,
    TERRAFORM_DIR_NAMES,
)

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


@pytest.fixture()
def deployment(mocker, snap):
    mocker.patch.object(manifest_mod, "Snap", return_value=snap)
    mocker.patch.object(deployment_mod, "Snap", return_value=snap)
    snap_config = {"deployment.risk": "stable"}
    snap.config.get.side_effect = snap_config.__getitem__
    with patch("sunbeam.jobs.deployment.Deployment") as p:
        dep = p(name="", url="", type="")
        dep.get_manifest.side_effect = functools.partial(Deployment.get_manifest, dep)
        dep.get_tfhelper.side_effect = functools.partial(Deployment.get_tfhelper, dep)
        dep._load_tfhelpers.side_effect = functools.partial(
            Deployment._load_tfhelpers, dep
        )
        dep.get_client.side_effect = ValueError("No clusterd in testing...")
        dep.__setattr__("_tfhelpers", {})
        dep._manifest = None
        dep.__setattr__("name", "test_deployment")
        yield dep


class TestDeployment:

    def test_get_default_manifest(self, deployment: Deployment):
        manifest = deployment.get_manifest()

        # Assert core charms / plans are present
        assert set(manifest.software.charms.keys()) >= MANIFEST_CHARM_VERSIONS.keys()
        assert set(manifest.software.terraform.keys()) >= TERRAFORM_DIR_NAMES.keys()

    def test_load_on_default(self, deployment: Deployment, tmpdir):
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        manifest_obj = deployment.get_manifest(manifest_file)

        # Check updates from manifest file
        ks_manifest = manifest_obj.software.charms["keystone-k8s"]
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Check default ones
        nova_manifest = manifest_obj.software.charms["nova-k8s"]
        assert nova_manifest.channel == OPENSTACK_CHANNEL
        assert nova_manifest.revision is None
        assert nova_manifest.config is None

    def test_load_latest_from_clusterdb(self, deployment: Deployment):
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        deployment.get_client.side_effect = None
        deployment.get_client.return_value = client
        manifest = deployment.get_manifest()
        ks_manifest = manifest.software.charms["keystone-k8s"]
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Assert defaults unchanged
        nova_manifest = manifest.software.charms["nova-k8s"]
        assert nova_manifest.channel == OPENSTACK_CHANNEL
        assert nova_manifest.revision is None
        assert nova_manifest.config is None

    def test_get_tfhelper(self, mocker, snap, copytree, deployment: Deployment):
        tfplan = "k8s-plan"
        mocker.patch.object(deployment_mod, "Snap", return_value=snap)
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        mocker.patch.object(terraform_mod, "Snap", return_value=snap)
        tfhelper = deployment.get_tfhelper(tfplan)
        assert tfhelper.plan == tfplan
        assert deployment._load_tfhelpers.call_count == 1
        copytree.assert_has_calls(
            [
                call(
                    Path(snap.paths.snap / "etc" / tfplan_dir),
                    Path(snap.paths.user_common / "etc" / deployment.name / tfplan_dir),
                    dirs_exist_ok=True,
                )
                for tfplan_dir in TERRAFORM_DIR_NAMES.values()
            ],
            any_order=True,
        )

    def test_get_tfhelper_tfplan_override_in_manifest(
        self, mocker, snap, copytree, deployment: Deployment
    ):
        tfplan = "openstack-plan"
        mocker.patch.object(deployment_mod, "Snap", return_value=snap)
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        mocker.patch.object(terraform_mod, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.side_effect = None
        deployment.get_client.return_value = client
        tfhelper = deployment.get_tfhelper(tfplan)
        tfplan_dir = TERRAFORM_DIR_NAMES.get(tfplan)
        test_manifest_dict = yaml.safe_load(test_manifest)
        copytree.assert_any_call(
            Path(
                test_manifest_dict["software"]["terraform"]["openstack-plan"]["source"]
            ),
            Path(snap.paths.user_common / "etc" / deployment.name / tfplan_dir),
            dirs_exist_ok=True,
        )
        assert tfhelper.plan == tfplan

    def test_get_tfhelper_multiple_calls(
        self, mocker, snap, copytree, deployment: Deployment
    ):
        tfplan = "k8s-plan"
        mocker.patch.object(deployment_mod, "Snap", return_value=snap)
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        mocker.patch.object(terraform_mod, "Snap", return_value=snap)
        tfhelper = deployment.get_tfhelper(tfplan)
        assert tfhelper.plan == tfplan
        assert deployment._load_tfhelpers.call_count == 1
        # _load_tfhelpers should be cached
        tfhelper = deployment.get_tfhelper(tfplan)
        assert deployment._load_tfhelpers.call_count == 1

    def test_get_tfhelper_missing_terraform_source(
        self, mocker, snap, copytree, deployment: Deployment
    ):
        tfplan = "openstack-plan"
        mocker.patch.object(deployment_mod, "Snap", return_value=snap)
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        mocker.patch.object(terraform_mod, "Snap", return_value=snap)
        deployment.get_manifest.side_effect = lambda: manifest_mod.Manifest()
        with pytest.raises(deployment_mod.MissingTerraformInfoException):
            deployment.get_tfhelper(tfplan)
        copytree.assert_not_called()
