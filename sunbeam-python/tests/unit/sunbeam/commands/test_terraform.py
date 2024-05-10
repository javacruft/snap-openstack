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
import functools
from unittest.mock import Mock, patch

import pytest

import sunbeam.commands.terraform as terraform_mod
import sunbeam.jobs.deployment as deployment_mod
import sunbeam.jobs.manifest as manifest_mod
from sunbeam.jobs.deployment import Deployment
from sunbeam.versions import OPENSTACK_CHANNEL

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
def deployment():
    with patch("sunbeam.jobs.deployment.Deployment") as p:
        dep = p(name="", url="", type="")
        dep.get_manifest.side_effect = functools.partial(Deployment.get_manifest, dep)
        dep.get_tfhelper.side_effect = functools.partial(Deployment.get_tfhelper, dep)
        dep._load_tfhelpers.side_effect = functools.partial(
            Deployment._load_tfhelpers, dep
        )
        dep.__setattr__("_tfhelpers", {})
        dep._manifest = None
        dep.__setattr__("name", "test_deployment")
        yield dep


@pytest.fixture()
def read_config():
    with patch("sunbeam.commands.terraform.read_config") as p:
        yield p


class TestTerraformHelper:
    def test_update_tfvars_and_apply_tf(
        self,
        mocker,
        snap,
        copytree,
        deployment: Deployment,
        read_config,
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
        mocker.patch.object(deployment_mod, "Snap", return_value=snap)
        mocker.patch.object(manifest_mod, "Snap", return_value=snap)
        mocker.patch.object(terraform_mod, "Snap", return_value=snap)
        client = Mock()
        client.cluster.get_latest_manifest.return_value = {"data": test_manifest}
        client.cluster.get_config.return_value = "{}"
        deployment.get_client.return_value = client
        manifest = deployment.get_manifest()

        tfhelper = deployment.get_tfhelper(tfplan)
        with patch.object(tfhelper, "write_tfvars") as write_tfvars, patch.object(
            tfhelper, "apply"
        ) as apply:
            tfhelper.update_tfvars_and_apply_tf(
                client, manifest, "fake-config", extra_tfvars
            )
            write_tfvars.assert_called_once()
            apply.assert_called_once()
            applied_tfvars = write_tfvars.call_args.args[0]

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
