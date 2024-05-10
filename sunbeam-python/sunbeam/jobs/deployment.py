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

import copy
import logging
import pathlib
import shutil
from typing import Type

import pydantic
import yaml
from juju.controller import Controller
from snaphelpers import Snap

import sunbeam.utils as sunbeam_utils
from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
)
from sunbeam.commands.terraform import TerraformHelper
from sunbeam.jobs.common import (
    RiskLevel,
    _get_default_no_proxy_settings,
    infer_risk,
    read_config,
)
from sunbeam.jobs.juju import JujuAccount, JujuController
from sunbeam.jobs.manifest import Manifest, embedded_manifest_path
from sunbeam.versions import MANIFEST_ATTRIBUTES_TFVAR_MAP, TERRAFORM_DIR_NAMES

LOG = logging.getLogger(__name__)
PROXY_CONFIG_KEY = "ProxySettings"

_cls_registry: dict[str, Type["Deployment"]] = {}


def register_deployment_type(type_: str, cls: Type["Deployment"]):
    global _cls_registry
    _cls_registry[type_] = cls


def get_deployment_class(type_: str) -> Type["Deployment"]:
    global _cls_registry
    return _cls_registry[type_]


class MissingTerraformInfoException(Exception):
    """An Exception raised when terraform information is missing in manifest"""

    pass


class Deployment(pydantic.BaseModel):
    name: str
    url: str
    type: str
    juju_account: JujuAccount | None = None
    juju_controller: JujuController | None = None
    _manifest: Manifest | None = pydantic.PrivateAttr(default=None)
    _tfhelpers: dict[str, TerraformHelper] = pydantic.PrivateAttr(default={})

    @property
    def infrastructure_model(self) -> str:
        """Return the infrastructure model name."""
        return NotImplemented

    @classmethod
    def load(cls, deployment: dict) -> "Deployment":
        """Load deployment from dict."""
        if type_ := deployment.get("type"):
            return _cls_registry.get(type_, Deployment)(**deployment)
        raise ValueError("Deployment type not set.")

    @classmethod
    def import_step(cls) -> Type:
        """Return a step for importing a deployment.

        This step will be used to make sure the deployment is valid.
        The step must take as constructor arguments: DeploymentsConfig, Deployment.
        The Deployment must be of the type that the step is registered for.
        """
        return NotImplemented  # type: ignore

    def get_client(self) -> Client:
        """Return a client instance

        Raises ValueError when fails to instantiate a client.
        """
        return NotImplemented  # type: ignore

    def get_clusterd_http_address(self) -> str:
        """Return the address of the clusterd server."""
        return NotImplemented  # type: ignore

    def get_connected_controller(self) -> Controller:
        """Return connected controller."""
        if self.juju_account is None:
            raise ValueError(f"No juju account configured for deployment {self.name}.")
        if self.juju_controller is None:
            raise ValueError(
                f"No juju controller configured for deployment {self.name}."
            )
        return self.juju_controller.to_controller(self.juju_account)

    def generate_preseed(self, console) -> str:
        """Generate preseed for deployment."""
        return NotImplemented

    def get_default_proxy_settings(self) -> dict:
        """Return default proxy settings."""
        return {}

    def get_plugin_manager(self) -> "PluginManager":
        """Return the plugin manager for the deployment."""
        from sunbeam.jobs.plugin import PluginManager

        plugin_manager = PluginManager()
        return plugin_manager

    def get_proxy_settings(self) -> dict:
        proxy = {}
        try:
            # If client does not exist, use detaults
            client = self.get_client()
            proxy_from_db = read_config(client, PROXY_CONFIG_KEY).get("proxy", {})
            if proxy_from_db.get("proxy_required"):
                proxy = {
                    p.upper(): v
                    for p in ("http_proxy", "https_proxy", "no_proxy")
                    if (v := proxy_from_db.get(p))
                }
        except (
            ClusterServiceUnavailableException,
            ConfigItemNotFoundException,
            ValueError,
        ) as e:
            LOG.debug(f"Using default Proxy settings from provider due to {str(e)}")
            proxy = self.get_default_proxy_settings()

        if "NO_PROXY" in proxy:
            no_proxy_list = set(proxy.get("NO_PROXY", "").split(","))
            default_no_proxy_list = _get_default_no_proxy_settings()
            proxy["NO_PROXY"] = ",".join(no_proxy_list.union(default_no_proxy_list))

        return proxy

    def get_manifest(self, manifest_file: pathlib.Path | None = None) -> Manifest:
        """Return the manifest for the deployment."""
        if self._manifest is not None:
            return self._manifest

        plugin_manager = self.get_plugin_manager()

        manifest = Manifest.get_default(plugin_manager.get_all_plugin_manifests(self))

        override_manifest = None
        if manifest_file is not None:
            override_manifest = Manifest.from_file(manifest_file)
            LOG.debug("Manifest loaded from file.")
        else:
            try:
                client = self.get_client()
                override_manifest = Manifest(
                    **yaml.safe_load(client.cluster.get_latest_manifest()["data"])
                )
                LOG.debug("Manifest loaded from clusterd.")
            except ClusterServiceUnavailableException:
                LOG.debug(
                    "Failed to get manifest from clusterd, might not be bootstrapped,"
                    " consider default manifest."
                )
            except ConfigItemNotFoundException:
                LOG.debug(
                    "No manifest found in clusterd, consider default"
                    " manifest from database."
                )
            except ValueError:
                LOG.debug(
                    "Failed to get clusterd client, might no be bootstrapped,"
                    " consider empty manifest from database."
                )
            if override_manifest is None:
                # Only get manifest from embedded if manifest not present in clusterd
                snap = Snap()
                risk = infer_risk(snap)
                if risk != RiskLevel.STABLE:
                    manifest_file = embedded_manifest_path(snap, risk)
                    LOG.debug(f"Risk {risk.value} detected, loading {manifest_file}...")
                    override_manifest = Manifest.from_file(manifest_file)
                    LOG.debug("Manifest loaded from embedded manifest.")

        if override_manifest is not None:
            override_manifest.validate_against_default(manifest)
            manifest = manifest.merge(override_manifest)

        # TODO(gboutry): Manage extra better
        plugin_manager.add_manifest_section(self, manifest.software)

        self._manifest = manifest
        return self._manifest

    def _load_tfhelpers(self):
        plugin_manager = self.get_plugin_manager()
        # TODO(gboutry): Remove snap instanciation
        snap = Snap()

        tfvar_map = copy.deepcopy(MANIFEST_ATTRIBUTES_TFVAR_MAP)
        tfvar_map_plugin = plugin_manager.get_all_plugin_manifest_tfvar_map(self)
        tfvar_map = sunbeam_utils.merge_dict(tfvar_map, tfvar_map_plugin)

        manifest = self.get_manifest()
        if not manifest.software.terraform:
            raise MissingTerraformInfoException("Manifest is missing terraform plans.")

        env = {}
        if self.juju_controller and self.juju_account:
            env.update(
                dict(
                    JUJU_USERNAME=self.juju_account.user,
                    JUJU_PASSWORD=self.juju_account.password,
                    JUJU_CONTROLLER_ADDRESSES=",".join(
                        self.juju_controller.api_endpoints
                    ),
                    JUJU_CA_CERT=self.juju_controller.ca_cert,
                )
            )
        env.update(self.get_proxy_settings())

        for tfplan, tf_manifest in manifest.software.terraform.items():
            tfplan_dir = TERRAFORM_DIR_NAMES.get(tfplan, tfplan)
            src = tf_manifest.source
            dst = snap.paths.user_common / "etc" / self.name / tfplan_dir
            LOG.debug(f"Updating {dst} from {src}...")
            shutil.copytree(src, dst, dirs_exist_ok=True)

            self._tfhelpers[tfplan] = TerraformHelper(
                path=dst,
                plan=tfplan,
                tfvar_map=tfvar_map.get(tfplan, {}),
                backend="http",
                env=env,
                clusterd_address=self.get_clusterd_http_address(),
            )

    def get_tfhelper(self, tfplan: str) -> TerraformHelper:
        if len(self._tfhelpers) == 0:
            self._load_tfhelpers()

        if tfhelper := self._tfhelpers.get(tfplan):
            return tfhelper

        raise ValueError(f"{tfplan} not found in tfhelpers")
