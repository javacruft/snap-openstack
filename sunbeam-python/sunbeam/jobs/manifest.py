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
import shutil
from dataclasses import InitVar, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field
from pydantic.dataclasses import dataclass
from snaphelpers import Snap

from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
    ManifestItemNotFoundException,
)
from sunbeam.commands.terraform import TerraformHelper
from sunbeam.jobs.common import (
    BaseStep,
    Result,
    ResultType,
    Status,
    get_proxy_settings,
    read_config,
    update_config,
)
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.plugin import PluginManager
from sunbeam.versions import (
    MANIFEST_ATTRIBUTES_TFVAR_MAP,
    MANIFEST_CHARM_VERSIONS,
    TERRAFORM_DIR_NAMES,
)

LOG = logging.getLogger(__name__)
EMPTY_MANIFEST = {"charms": {}, "terraform": {}}


class MissingTerraformInfoException(Exception):
    """An Exception raised when terraform information is missing in manifest"""

    pass


@dataclass
class JujuManifest:
    # Setting Field alias not supported in pydantic 1.10.0
    # Old version of pydantic is used due to dependencies
    # with older version of paramiko from python-libjuju
    # Newer version of pydantic can be used once the below
    # PR is released
    # https://github.com/juju/python-libjuju/pull/1005
    bootstrap_args: list[str] = Field(
        default=[], description="Extra args for juju bootstrap"
    )
    scale_args: list[str] = Field(
        default=[], description="Extra args for juju enable-ha"
    )


@dataclass
class CharmsManifest:
    channel: Optional[str] = Field(default=None, description="Channel for the charm")
    revision: Optional[int] = Field(
        default=None, description="Revision number of the charm"
    )
    # rocks: Optional[Dict[str, str]] = Field(
    #     default=None, description="Rock images for the charm"
    # )
    config: Optional[Dict[str, Any]] = Field(
        default=None, description="Config options of the charm"
    )
    # source: Optional[Path] = Field(
    #     default=None, description="Local charm bundle path"
    # )


@dataclass
class TerraformManifest:
    source: Path = Field(description="Path to Terraform plan")


@dataclass(config=dict(extra="allow"))
class SoftwareConfig:
    deployment: InitVar[Deployment]
    plugin_manager: InitVar[PluginManager]
    juju: Optional[JujuManifest] = None
    charms: Optional[Dict[str, CharmsManifest]] = None
    terraform: Optional[Dict[str, TerraformManifest]] = None

    """
    # field_validator supported only in pydantix 2.x
    @field_validator("terraform", "mode_after")
    def validate_terraform(cls, terraform):
        if terraform:
            tf_keys = list(terraform.keys())
            if not set(tf_keys) <= set(VALID_TERRAFORM_PLANS):
                raise ValueError(
                    f"Terraform keys should be one of {VALID_TERRAFORM_PLANS}"
                )

        return terraform
    """

    def validate_terraform_keys(self, default_software_config: dict):
        if self.terraform:
            tf_keys = set(self.terraform.keys())
            all_tfplans = default_software_config.get("terraform", {}).keys()
            if not tf_keys <= all_tfplans:
                raise ValueError(
                    f"Manifest Software Terraform keys should be one of {all_tfplans} "
                )

    def validate_charm_keys(self, default_software_config: dict):
        if self.charms:
            charms_keys = set(self.charms.keys())
            all_charms = default_software_config.get("charms", {}).keys()
            if not charms_keys <= all_charms:
                raise ValueError(
                    f"Manifest Software charms keys should be one of {all_charms} "
                )

    def __post_init__(self, deployment: Deployment, plugin_manager: PluginManager):
        LOG.debug("Calling __post__init__")
        plugin_manager.add_manifest_section(deployment, self)
        default_software_config = self.get_default_software_as_dict(
            deployment, plugin_manager
        )
        # Add custom validations
        self.validate_terraform_keys(default_software_config)
        self.validate_charm_keys(default_software_config)

    @classmethod
    def get_default_software_as_dict(
        cls, deployment: Deployment, plugin_manager: PluginManager
    ) -> dict:
        snap = Snap()
        software = {"juju": {"bootstrap_args": []}}
        software["charms"] = {
            charm: {"channel": channel}
            for charm, channel in MANIFEST_CHARM_VERSIONS.items()
        }
        software["terraform"] = {
            tfplan: {"source": Path(snap.paths.snap / "etc" / tfplan_dir)}
            for tfplan, tfplan_dir in TERRAFORM_DIR_NAMES.items()
        }

        # Update manifests from plugins
        software_from_plugins = plugin_manager.get_all_plugin_manifests(deployment)
        utils.merge_dict(software, software_from_plugins)
        return copy.deepcopy(software)


class Manifest:

    def __init__(
        self,
        deployment: Deployment,
        plugin_manager: PluginManager,
        deployment_config: dict,
        software: dict,
    ):
        self.deployment = deployment
        self.plugin_manager = plugin_manager
        self.deployment_config = deployment_config
        self.software_config = SoftwareConfig(deployment, plugin_manager, **software)
        self.tf_helpers = {}
        self.tfvar_map = self._get_all_tfvar_map(deployment, plugin_manager)

    @classmethod
    def load(
        cls, deployment: Deployment, manifest_file: Path, include_defaults: bool = False
    ) -> "Manifest":
        """Load the manifest with the provided file input

        If include_defaults is True, load the manifest over the defaut manifest.
        """
        if include_defaults:
            return cls.load_on_default(deployment, manifest_file)

        plugin_manager = PluginManager()
        with manifest_file.open() as file:
            override = yaml.safe_load(file)
            return Manifest(
                deployment,
                plugin_manager,
                override.get("deployment", {}),
                override.get("software", {}),
            )

    @classmethod
    def load_latest_from_clusterdb(
        cls, deployment: Deployment, include_defaults: bool = False
    ) -> "Manifest":
        """Load the latest manifest from clusterdb

        If include_defaults is True, load the manifest over the defaut manifest.
        values.
        """
        if include_defaults:
            return cls.load_latest_from_clusterdb_on_default(deployment)

        plugin_manager = PluginManager()
        try:
            manifest_latest = deployment.get_client().cluster.get_latest_manifest()
            override = yaml.safe_load(manifest_latest.get("data"))
            return Manifest(
                deployment,
                plugin_manager,
                override.get("deployment", {}),
                override.get("software", {}),
            )
        except ManifestItemNotFoundException as e:
            LOG.debug(f"Error in getting latest manifest from cluster DB: {str(e)}")
            return Manifest(deployment, plugin_manager, {}, {})

    @classmethod
    def load_on_default(cls, deployment: Deployment, manifest_file: Path) -> "Manifest":
        """Load manifest and override the default manifest"""
        plugin_manager = PluginManager()
        with manifest_file.open() as file:
            override = yaml.safe_load(file)
            override_deployment = override.get("deployment") or {}
            override_software = override.get("software") or {}
            default_software = SoftwareConfig.get_default_software_as_dict(
                deployment, plugin_manager
            )
            utils.merge_dict(default_software, override_software)
            return Manifest(
                deployment, plugin_manager, override_deployment, default_software
            )

    @classmethod
    def load_latest_from_clusterdb_on_default(
        cls, deployment: Deployment
    ) -> "Manifest":
        """Load the latest manifest from clusterdb"""
        plugin_manager = PluginManager()
        default_software = SoftwareConfig.get_default_software_as_dict(
            deployment, plugin_manager
        )
        try:
            manifest_latest = deployment.get_client().cluster.get_latest_manifest()
            override = yaml.safe_load(manifest_latest.get("data"))
        except ManifestItemNotFoundException as e:
            LOG.debug(f"Error in getting latest manifest from cluster DB: {str(e)}")
            override = {}

        override_deployment = override.get("deployment") or {}
        override_software = override.get("software") or {}
        utils.merge_dict(default_software, override_software)
        return Manifest(
            deployment, plugin_manager, override_deployment, default_software
        )

    @classmethod
    def get_default_manifest(cls, deployment: Deployment) -> "Manifest":
        plugin_manager = PluginManager()
        default_software = SoftwareConfig.get_default_software_as_dict(
            deployment, plugin_manager
        )
        return Manifest(deployment, plugin_manager, {}, default_software)

    def _get_all_tfvar_map(
        self, deployment: Deployment, plugin_manager: PluginManager
    ) -> dict:
        tfvar_map = copy.deepcopy(MANIFEST_ATTRIBUTES_TFVAR_MAP)
        tfvar_map_plugin = plugin_manager.get_all_plugin_manfiest_tfvar_map(deployment)
        utils.merge_dict(tfvar_map, tfvar_map_plugin)
        return tfvar_map

    # Terraform helper classes
    def get_tfhelper(self, tfplan: str) -> TerraformHelper:
        snap = Snap()
        if self.tf_helpers.get(tfplan):
            return self.tf_helpers.get(tfplan)

        if not (
            self.software_config.terraform
            and self.software_config.terraform.get(tfplan)  # noqa W503
        ):
            raise MissingTerraformInfoException(
                f"Terraform information missing in manifest for {tfplan}"
            )

        tfplan_dir = TERRAFORM_DIR_NAMES.get(tfplan, tfplan)
        src = self.software_config.terraform.get(tfplan).source
        dst = snap.paths.user_common / "etc" / self.deployment.name / tfplan_dir
        LOG.debug(f"Updating {dst} from {src}...")
        shutil.copytree(src, dst, dirs_exist_ok=True)
        env = {}
        if self.deployment.juju_controller and self.deployment.juju_account:
            env.update(
                dict(
                    JUJU_USERNAME=self.deployment.juju_account.user,
                    JUJU_PASSWORD=self.deployment.juju_account.password,
                    JUJU_CONTROLLER_ADDRESSES=",".join(
                        self.deployment.juju_controller.api_endpoints
                    ),
                    JUJU_CA_CERT=self.deployment.juju_controller.ca_cert,
                )
            )
        env.update(get_proxy_settings(self.deployment))

        self.tf_helpers[tfplan] = TerraformHelper(
            path=dst,
            plan=tfplan,
            backend="http",
            env=env,
            clusterd_address=self.deployment.get_clusterd_http_address(),
        )

        return self.tf_helpers[tfplan]

    def update_partial_tfvars_and_apply_tf(
        self,
        client: Client,
        charms: List[str],
        tfplan: str,
        tfvar_config: Optional[str] = None,
        tf_apply_extra_args: list | None = None,
    ) -> None:
        """Updates tfvars for specific charms and apply the plan."""
        current_tfvars = {}
        updated_tfvars = {}
        if tfvar_config:
            try:
                current_tfvars = read_config(client, tfvar_config)
                # Exclude all default tfvar keys from the previous terraform
                # vars applied to the plan.
                _tfvar_names = self._get_tfvar_names(tfplan, charms)
                updated_tfvars = {
                    k: v for k, v in current_tfvars.items() if k not in _tfvar_names
                }
            except ConfigItemNotFoundException:
                pass

        updated_tfvars.update(self._get_tfvars(tfplan, charms))
        if tfvar_config:
            update_config(client, tfvar_config, updated_tfvars)

        tfhelper = self.get_tfhelper(tfplan)
        tfhelper.write_tfvars(updated_tfvars)
        LOG.debug(f"Applying plan {tfplan} with tfvars {updated_tfvars}")
        tfhelper.apply(tf_apply_extra_args)

    def update_tfvars_and_apply_tf(
        self,
        client: Client,
        tfplan: str,
        tfvar_config: Optional[str] = None,
        override_tfvars: dict = {},
        tf_apply_extra_args: list | None = None,
    ) -> None:
        """Updates terraform vars and Apply the terraform.

        Get tfvars from cluster db using tfvar_config key, Manifest file using
        Charm Manifest tfvar map from core and plugins, User provided override_tfvars.
        Merge the tfvars in the above order so that terraform vars in override_tfvars
        will have highest priority.
        Get tfhelper object for tfplan and write tfvars and apply the terraform plan.

        :param tfplan: Terraform plan to use to get tfhelper
        :type tfplan: str
        :param tfvar_config: TerraformVar key name used to save tfvar in clusterdb
        :type tfvar_config: str or None
        :param override_tfvars: Terraform vars to override
        :type override_tfvars: dict
        :param tf_apply_extra_args: Extra args to terraform apply command
        :type tf_apply_extra_args: list or None
        """
        current_tfvars = None
        updated_tfvars = {}
        if tfvar_config:
            try:
                current_tfvars = read_config(client, tfvar_config)
                # Exclude all default tfvar keys from the previous terraform
                # vars applied to the plan.
                _tfvar_names = self._get_tfvar_names(tfplan)
                updated_tfvars = {
                    k: v for k, v in current_tfvars.items() if k not in _tfvar_names
                }
            except ConfigItemNotFoundException:
                pass

        # NOTE: It is expected for Manifest to contain all previous changes
        # So override tfvars from configdb to defaults if not specified in
        # manifest file
        updated_tfvars.update(self._get_tfvars(tfplan))
        updated_tfvars.update(override_tfvars)
        if tfvar_config:
            update_config(client, tfvar_config, updated_tfvars)

        tfhelper = self.get_tfhelper(tfplan)
        tfhelper.write_tfvars(updated_tfvars)
        LOG.debug(f"Applying plan {tfplan} with tfvars {updated_tfvars}")
        tfhelper.apply(tf_apply_extra_args)

    def _get_tfvars(self, tfplan: str, charms: Optional[list] = None) -> dict:
        """Get tfvars from the manifest.

        MANIFEST_ATTRIBUTES_TFVAR_MAP holds the mapping of Manifest attributes
        and the terraform variable name. For each terraform variable in
        MANIFEST_ATTRIBUTES_TFVAR_MAP, get the corresponding value from Manifest
        and return all terraform variables as dict.

        If charms is passed as input, filter the charms based on the list
        provided.
        """
        tfvars = {}

        charms_tfvar_map = self.tfvar_map.get(tfplan, {}).get("charms", {})
        if charms:
            charms_tfvar_map = {
                k: v for k, v in charms_tfvar_map.items() if k in charms
            }

        # handle tfvars for charms section
        for charm, per_charm_tfvar_map in charms_tfvar_map.items():
            charm_ = self.software_config.charms.get(charm)
            if charm_:
                manifest_charm = asdict(charm_)
                for charm_attribute, tfvar_name in per_charm_tfvar_map.items():
                    charm_attribute_ = manifest_charm.get(charm_attribute)
                    if charm_attribute_:
                        tfvars[tfvar_name] = charm_attribute_

        return tfvars

    def _get_tfvar_names(self, tfplan: str, charms: Optional[list] = None) -> list:
        if charms:
            return [
                tfvar_name
                for charm, per_charm_tfvar_map in self.tfvar_map.get(tfplan, {})
                .get("charms", {})
                .items()
                for charm_attribute, tfvar_name in per_charm_tfvar_map.items()
                if charm in charms
            ]
        else:
            return [
                tfvar_name
                for charm, per_charm_tfvar_map in self.tfvar_map.get(tfplan, {})
                .get("charms", {})
                .items()
                for charm_attribute, tfvar_name in per_charm_tfvar_map.items()
            ]


class AddManifestStep(BaseStep):
    """Add Manifest file to cluster database"""

    def __init__(self, client: Client, manifest: Optional[Path] = None):
        super().__init__("Write Manifest to database", "Writing Manifest to database")
        # Write EMPTY_MANIFEST if manifest not provided
        self.manifest = manifest
        self.client = client
        self.manifest_content = None

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Skip if the user provided manifest and the latest from db are same."""
        try:
            if self.manifest:
                with self.manifest.open("r") as file:
                    self.manifest_content = yaml.safe_load(file)
            else:
                self.manifest_content = EMPTY_MANIFEST

            latest_manifest = self.client.cluster.get_latest_manifest()
        except ManifestItemNotFoundException:
            return Result(ResultType.COMPLETED)
        except (ClusterServiceUnavailableException, yaml.YAMLError, IOError) as e:
            LOG.debug(e)
            return Result(ResultType.FAILED, str(e))

        if yaml.safe_load(latest_manifest.get("data")) == self.manifest_content:
            return Result(ResultType.SKIPPED)

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Write manifest to cluster db"""
        try:
            id = self.client.cluster.add_manifest(
                data=yaml.safe_dump(self.manifest_content)
            )
            return Result(ResultType.COMPLETED, id)
        except Exception as e:
            LOG.debug(e)
            return Result(ResultType.FAILED, str(e))
