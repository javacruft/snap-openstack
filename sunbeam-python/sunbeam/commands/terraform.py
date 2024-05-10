# Copyright (c) 2023 Canonical Ltd.
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

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Optional

from rich.status import Status
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.jobs.common import BaseStep, Result, ResultType, read_config, update_config
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)

http_backend_template = """
terraform {
  backend "http" {
    address                = $address
    update_method          = $update_method
    lock_address           = $lock_address
    lock_method            = $lock_method
    unlock_address         = $unlock_address
    unlock_method          = $unlock_method
    skip_cert_verification = $skip_cert_verification
  }
}
"""

terraform_rc_template = """
disable_checkpoint = true
provider_installation {
  filesystem_mirror {
    path    = "$snap_path/usr/share/terraform-providers"
  }
}
"""


class TerraformException(Exception):
    """Terraform related exceptions"""

    def __init__(self, message):
        super().__init__()
        self.message = message

    def __str__(self) -> str:
        return self.message


class TerraformHelper:
    """Helper for interaction with Terraform"""

    def __init__(
        self,
        path: Path,
        plan: str,
        tfvar_map: dict,
        env: Optional[dict] = None,
        parallelism: Optional[int] = None,
        backend: Optional[str] = None,
        clusterd_address: str | None = None,
    ):
        self.snap = Snap()
        self.path = path
        self.plan = plan
        self.tfvar_map = tfvar_map
        self.env = env
        self.parallelism = parallelism
        self.backend = backend or "local"
        self.terraform = str(self.snap.paths.snap / "bin" / "terraform")
        self.clusterd_address = clusterd_address

    def backend_config(self) -> dict:
        if self.backend == "http" and self.clusterd_address is not None:
            address = self.clusterd_address
            return {
                "address": f"{address}/1.0/terraformstate/{self.plan}",
                "update_method": "PUT",
                "lock_address": f"{address}/1.0/terraformlock/{self.plan}",
                "lock_method": "PUT",
                "unlock_address": f"{address}/1.0/terraformunlock/{self.plan}",
                "unlock_method": "PUT",
                "skip_cert_verification": True,
            }
        return {}

    def write_backend_tf(self) -> bool:
        backend = self.backend_config()
        if self.backend == "http":
            backend_obj = Template(http_backend_template)
            backend = backend_obj.safe_substitute(
                {key: json.dumps(value) for key, value in backend.items()}
            )
            backend_path = self.path / "backend.tf"
            old_backend = None
            if backend_path.exists():
                old_backend = backend_path.read_text()
            if old_backend != backend:
                with backend_path.open(mode="w") as file:
                    file.write(backend)
                return True
        return False

    def write_tfvars(self, vars: dict, location: Optional[Path] = None) -> None:
        """Write terraform variables file"""
        filepath = location or (self.path / "terraform.tfvars.json")
        with filepath.open("w") as tfvars:
            tfvars.write(json.dumps(vars))

    def write_terraformrc(self) -> None:
        """Write .terraformrc file"""
        terraform_rc = self.snap.paths.user_data / ".terraformrc"
        with terraform_rc.open(mode="w") as file:
            file.write(
                Template(terraform_rc_template).safe_substitute(
                    {"snap_path": self.snap.paths.snap}
                )
            )

    def init(self) -> None:
        """terraform init"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-init-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)
        backend_updated = False
        if self.backend:
            backend_updated = self.write_backend_tf()
        self.write_terraformrc()

        try:
            cmd = [self.terraform, "init", "-upgrade", "-no-color"]
            if backend_updated:
                LOG.debug("Backend updated, running terraform init -reconfigure")
                cmd.append("-reconfigure")
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform init failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def apply(self, extra_args: list | None = None):
        """terraform apply"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-apply-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)

        try:
            cmd = [self.terraform, "apply"]
            if extra_args:
                cmd.extend(extra_args)
            cmd.extend(["-auto-approve", "-no-color"])
            if self.parallelism is not None:
                cmd.append(f"-parallelism={self.parallelism}")
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform apply failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def destroy(self):
        """terraform destroy"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-destroy-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)

        try:
            cmd = [self.terraform, "destroy", "-auto-approve", "-no-color"]
            if self.parallelism is not None:
                cmd.append(f"-parallelism={self.parallelism}")
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform destroy failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def output(self, hide_output: bool = False) -> dict:
        """terraform output"""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-output-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)

        try:
            cmd = [self.terraform, "output", "-json", "-no-color"]
            LOG.debug(f'Running command {" ".join(cmd)}')
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            stdout = process.stdout
            output = ""
            if not hide_output:
                output = f" stdout={stdout}, stderr={process.stderr}"
            LOG.debug("Command finished." + output)
            tf_output = json.loads(stdout)
            output = {}
            for key, value in tf_output.items():
                output[key] = value["value"]
            return output
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform output failed: {e.output}")
            LOG.warning(e.stderr)
            raise TerraformException(str(e))

    def sync(self) -> None:
        """Sync the running state back to the Terraform state file."""
        os_env = os.environ.copy()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        tf_log = str(self.path / f"terraform-sync-{timestamp}.log")
        os_env.update({"TF_LOG_PATH": tf_log})
        os_env.setdefault("TF_LOG", "INFO")
        if self.env:
            os_env.update(self.env)

        try:
            cmd = [self.terraform, "apply", "-refresh-only", "-auto-approve"]
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=self.path,
                env=os_env,
            )
            LOG.debug(
                f"Command finished. stdout={process.stdout}, stderr={process.stderr}"
            )
        except subprocess.CalledProcessError as e:
            LOG.error(f"terraform sync failed: {e.output}")
            LOG.error(e.stderr)
            raise TerraformException(str(e))

    def update_partial_tfvars_and_apply_tf(
        self,
        client: Client,
        manifest: Manifest,
        charms: list[str],
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
                _tfvar_names = self._get_tfvar_names(charms)
                updated_tfvars = {
                    k: v for k, v in current_tfvars.items() if k not in _tfvar_names
                }
            except ConfigItemNotFoundException:
                pass

        updated_tfvars.update(self._get_tfvars(manifest, charms))
        if tfvar_config:
            update_config(client, tfvar_config, updated_tfvars)

        self.write_tfvars(updated_tfvars)
        LOG.debug(f"Applying plan {self.plan} with tfvars {updated_tfvars}")
        self.apply(tf_apply_extra_args)

    def update_tfvars_and_apply_tf(
        self,
        client: Client,
        manifest: Manifest,
        tfvar_config: Optional[str] = None,
        override_tfvars: dict | None = None,
        tf_apply_extra_args: list | None = None,
    ) -> None:
        """Updates terraform vars and Apply the terraform.

        Get tfvars from cluster db using tfvar_config key, Manifest file using
        Charm Manifest tfvar map from core and plugins, User provided override_tfvars.
        Merge the tfvars in the above order so that terraform vars in override_tfvars
        will have highest priority.
        Get tfhelper object for tfplan and write tfvars and apply the terraform plan.

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
                _tfvar_names = self._get_tfvar_names()
                updated_tfvars = {
                    k: v for k, v in current_tfvars.items() if k not in _tfvar_names
                }
            except ConfigItemNotFoundException:
                pass

        # NOTE: It is expected for Manifest to contain all previous changes
        # So override tfvars from configdb to defaults if not specified in
        # manifest file
        updated_tfvars.update(self._get_tfvars(manifest))
        if override_tfvars:
            updated_tfvars.update(override_tfvars)
        if tfvar_config:
            update_config(client, tfvar_config, updated_tfvars)

        self.write_tfvars(updated_tfvars)
        LOG.debug(f"Applying plan {self.plan} with tfvars {updated_tfvars}")
        self.apply(tf_apply_extra_args)

    def _get_tfvars(self, manifest: Manifest, charms: Optional[list] = None) -> dict:
        """Get tfvars from the manifest.

        MANIFEST_ATTRIBUTES_TFVAR_MAP holds the mapping of Manifest attributes
        and the terraform variable name. For each terraform variable in
        MANIFEST_ATTRIBUTES_TFVAR_MAP, get the corresponding value from Manifest
        and return all terraform variables as dict.

        If charms is passed as input, filter the charms based on the list
        provided.
        """
        tfvars = {}

        charms_tfvar_map = self.tfvar_map.get("charms", {})
        if charms:
            charms_tfvar_map = {
                k: v for k, v in charms_tfvar_map.items() if k in charms
            }

        # handle tfvars for charms section
        for charm, per_charm_tfvar_map in charms_tfvar_map.items():
            charm_manifest = manifest.software.charms.get(charm)
            if charm_manifest:
                manifest_charm = charm_manifest.model_dump()
                for charm_attribute_name, tfvar_name in per_charm_tfvar_map.items():
                    charm_attribute_value = manifest_charm.get(charm_attribute_name)
                    if charm_attribute_value:
                        tfvars[tfvar_name] = charm_attribute_value

        return tfvars

    def _get_tfvar_names(self, charms: Optional[list] = None) -> list:
        if charms:
            return [
                tfvar_name
                for charm, per_charm_tfvar_map in self.tfvar_map.get(
                    "charms", {}
                ).items()
                for _, tfvar_name in per_charm_tfvar_map.items()
                if charm in charms
            ]
        else:
            return [
                tfvar_name
                for _, per_charm_tfvar_map in self.tfvar_map.get("charms", {}).items()
                for _, tfvar_name in per_charm_tfvar_map.items()
            ]


class TerraformInitStep(BaseStep):
    """Initialize Terraform with required providers."""

    def __init__(self, tfhelper: TerraformHelper):
        super().__init__(
            "Initialize Terraform", "Initializing Terraform from provider mirror"
        )
        self.tfhelper = tfhelper

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Initialise Terraform configuration from provider mirror,"""
        try:
            self.tfhelper.init()
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))
