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
from unittest.mock import Mock

import pytest
import yaml
import yaml.scanner
from pydantic import ValidationError

import sunbeam.jobs.manifest as manifest_mod
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ManifestItemNotFoundException,
)
from sunbeam.jobs.common import ResultType

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

test_manifest_incorrect_terraform_key = {
    "software": {
        "charms": {
            "keystone-k8s": {
                "channel": "2023.1/stable",
                "revision": 234,
                "config": {"debug": True},
            }
        },
        "terraform": {
            "fake-plan": {"source": "/home/ubuntu/tfplan"},
        },
    }
}


@pytest.fixture()
def snap_risk(snap, mocker):
    snap.config.get.side_effect = lambda key: "stable"
    mocker.patch.object(manifest_mod, "Snap", return_value=snap)
    return snap


class TestSoftwareConfig:
    def test_merge(self):
        config1 = manifest_mod.SoftwareConfig(
            charms={"my-charm-1": manifest_mod.CharmManifest()}
        )
        config2 = manifest_mod.SoftwareConfig(
            charms={"my-charm-2": manifest_mod.CharmManifest(channel="37")}
        )
        result = config1.merge(config2)
        assert result.charms == {
            "my-charm-1": manifest_mod.CharmManifest(),
            "my-charm-2": manifest_mod.CharmManifest(channel="37"),
        }


class TestManifest:
    def test_merge(self):
        manifest1 = manifest_mod.Manifest(
            software=manifest_mod.SoftwareConfig(
                charms={"my-charm-1": manifest_mod.CharmManifest()}
            )
        )
        manifest2 = manifest_mod.Manifest(
            software=manifest_mod.SoftwareConfig(
                charms={"my-charm-2": manifest_mod.CharmManifest(channel="37")}
            )
        )
        software_merged = manifest1.software.merge(manifest2.software)
        result = manifest_mod.Manifest.merge(manifest1, manifest2)
        assert result.software == software_merged

    def test_load(self, mocker, snap, tmpdir):
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        manifest_obj = manifest_mod.Manifest.from_file(manifest_file)
        ks_manifest = manifest_obj.software.charms["keystone-k8s"]
        assert ks_manifest.channel == "2023.1/stable"
        assert ks_manifest.revision == 234
        assert ks_manifest.config == {"debug": True}

        # Assert defaults does not exist
        assert "nova" not in manifest_obj.software.charms.keys()

        test_manifest_dict = yaml.safe_load(test_manifest)
        assert (
            manifest_obj.software.juju.bootstrap_args
            == test_manifest_dict["software"]["juju"]["bootstrap_args"]  # noqa: W503
        )

    def test_malformed_manifest(self, mocker, snap, tmpdir):
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(malformed_test_manifest)
        with pytest.raises(yaml.scanner.ScannerError):
            manifest_mod.Manifest.from_file(manifest_file)

    def test_load_manifest_invalid_values(self, mocker, snap, tmpdir):
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest_invalid_values)
        with pytest.raises(ValidationError):
            manifest_mod.Manifest.from_file(manifest_file)

    def test_validate_terraform_keys(self):
        manifest_ = manifest_mod.Manifest.model_validate(
            test_manifest_incorrect_terraform_key
        )
        with pytest.raises(ValueError):
            manifest_.validate_against_default(
                manifest_mod.Manifest(
                    software=manifest_mod.SoftwareConfig(
                        terraform={
                            "openstack-plan": manifest_mod.TerraformManifest(
                                source=Path("...")
                            )
                        }
                    )
                )
            )


class TestAddManifestStep:
    def test_is_skip(self, tmpdir, snap_risk):
        # Manifest in cluster DB different from user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": "charms: {}"}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest_mod.AddManifestStep(client, manifest_file)
        result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_apply_same_manifest(self, tmpdir, snap_risk):
        # Manifest in cluster DB same as user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest_mod.AddManifestStep(client, manifest_file)
        result = step.is_skip()

        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_clear(self, snap_risk):
        # Manifest in cluster DB same as user provided manifest
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        step = manifest_mod.AddManifestStep(client, clear=True)
        result = step.is_skip()

        assert step.manifest_content == manifest_mod.EMPTY_MANIFEST
        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_no_manifest_apply_same(self, snap_risk):
        # Manifest in cluster DB same as user provided manifest
        empty_manifest_str = yaml.safe_dump(manifest_mod.EMPTY_MANIFEST)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": empty_manifest_str}
        step = manifest_mod.AddManifestStep(client)
        result = step.is_skip()

        assert result.result_type == ResultType.SKIPPED

    def test_is_skip_no_connection_to_clusterdb(self, snap_risk):
        client = Mock()
        client.cluster.get_latest_manifest.side_effect = (
            ClusterServiceUnavailableException("Cluster unavailable..")
        )
        step = manifest_mod.AddManifestStep(client, clear=True)
        result = step.is_skip()

        assert result.result_type == ResultType.FAILED

    def test_is_skip_with_no_manifest_in_db(self, snap_risk):
        client = Mock()
        client.cluster.get_latest_manifest.side_effect = ManifestItemNotFoundException(
            "Manifest Item not found."
        )
        step = manifest_mod.AddManifestStep(client, clear=True)
        result = step.is_skip()

        assert result.result_type == ResultType.COMPLETED

    def test_is_skip_no_manifest_risk_edge(self, snap_risk, mocker):
        snap_risk.config.get.side_effect = lambda key: "edge"
        edge_manifest = snap_risk.paths.snap / "etc" / "manifests" / "edge.yml"
        edge_manifest.parent.mkdir(parents=True, exist_ok=True)
        edge_manifest.write_text(test_manifest)
        mocker.patch.object(
            manifest_mod, "embedded_manifest_path", return_value=edge_manifest
        )
        client = Mock()
        client.cluster.get_latest_manifest.side_effect = ManifestItemNotFoundException(
            "Manifest Item not found."
        )
        step = manifest_mod.AddManifestStep(client)
        result = step.is_skip()

        manifest_mod.embedded_manifest_path.assert_called_once_with(snap_risk, "edge")
        assert result.result_type == ResultType.COMPLETED

    def test_run(self, tmpdir, snap_risk):
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": "charms: {}"}
        manifest_file = tmpdir.mkdir("manifests").join("test_manifest.yaml")
        manifest_file.write(test_manifest)
        step = manifest_mod.AddManifestStep(client, manifest_file)
        step.manifest_content = yaml.safe_load(test_manifest)
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(step.manifest_content)
        )
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_no_manifest(self, snap_risk):
        client = Mock()
        step = manifest_mod.AddManifestStep(client)
        step.manifest_content = manifest_mod.EMPTY_MANIFEST
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(manifest_mod.EMPTY_MANIFEST)
        )
        assert result.result_type == ResultType.COMPLETED

    def test_run_with_no_connection_to_clusterdb(self, snap_risk):
        client = Mock()
        client.cluster.add_manifest.side_effect = ClusterServiceUnavailableException(
            "Cluster unavailable.."
        )
        step = manifest_mod.AddManifestStep(client)
        step.manifest_content = manifest_mod.EMPTY_MANIFEST
        result = step.run()

        client.cluster.add_manifest.assert_called_once_with(
            data=yaml.safe_dump(manifest_mod.EMPTY_MANIFEST)
        )
        assert result.result_type == ResultType.FAILED
