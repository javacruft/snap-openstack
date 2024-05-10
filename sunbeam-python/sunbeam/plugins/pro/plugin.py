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

"""Ubuntu Pro subscription management plugin."""

import logging
from pathlib import Path
from typing import Optional

import click
from packaging.version import Version
from rich.console import Console
from rich.status import Status
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.jobs.common import BaseStep, Result, ResultType, run_plan
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper, TimeoutException, run_sync
from sunbeam.jobs.manifest import Manifest, SoftwareConfig, TerraformManifest
from sunbeam.plugins.interface.v1.base import EnableDisablePlugin

LOG = logging.getLogger(__name__)
console = Console()

APPLICATION = "ubuntu-pro"
APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
UNIT_TIMEOUT = 1200  # 15 minutes, adding / removing units can take a long time


class EnableUbuntuProApplicationStep(BaseStep, JujuStepHelper):
    """Enable Ubuntu Pro application using Terraform"""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        token: str,
        model: str,
    ):
        super().__init__("Enable Ubuntu Pro", "Enabling Ubuntu Pro support")
        self.client = client
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = manifest
        self.token = token
        self.model = model

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user."""
        return False

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Apply terraform configuration to deploy ubuntu-pro"""
        extra_tfvars = {"token": self.token}
        try:
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=None,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))

        # Note(gboutry): application is in state unknown when it's deployed
        # without units
        try:
            run_sync(
                self.jhelper.wait_application_ready(
                    APPLICATION,
                    self.model,
                    accepted_status=["active", "blocked", "unknown"],
                    timeout=APP_TIMEOUT,
                )
            )

            # Check status of pro application for any token issues
            pro_app = run_sync(
                self.jhelper.get_application(
                    APPLICATION,
                    self.model,
                )
            )
            if pro_app.status == "blocked":
                message = "unknown error"
                for unit in pro_app.units:
                    if "invalid token" in unit.workload_status_message:
                        message = "invalid token"
                LOG.warning(f"Unable to enable Ubuntu Pro: {message}")
                return Result(ResultType.FAILED, message)
        except TimeoutException as e:
            LOG.warning(str(e))
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class DisableUbuntuProApplicationStep(BaseStep, JujuStepHelper):
    """Disable Ubuntu Pro application using Terraform"""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        manifest: Manifest,
    ):
        super().__init__("Disable Ubuntu Pro", "Disabling Ubuntu Pro support")
        self.client = client
        self.tfhelper = tfhelper
        self.manifest = manifest

    def has_prompts(self) -> bool:
        """Returns true if the step has prompts that it can ask the user."""
        return False

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Apply terraform configuration to disable ubuntu-pro"""
        extra_tfvars = {"token": ""}
        try:
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=None,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class ProPlugin(EnableDisablePlugin):
    version = Version("0.0.1")

    def __init__(self, deployment: Deployment) -> None:
        super().__init__("pro", deployment)
        self.token = None
        self.snap = Snap()
        self.tfplan = "ubuntu-pro-plan"
        self.tfplan_dir = f"deploy-{self.name}"
        self._manifest = None

    @property
    def manifest(self) -> Manifest:
        if self._manifest:
            return self._manifest

        self._manifest = self.deployment.get_manifest()
        return self._manifest

    def manifest_defaults(self) -> SoftwareConfig:
        """Plugin software configuration"""
        return SoftwareConfig(
            terraform={
                self.tfplan: TerraformManifest(
                    source=Path(__file__).parent / "etc" / self.tfplan_dir
                )
            }
        )

    def run_enable_plans(self):
        if self.token is None:
            raise ValueError("Token is required to enable Ubuntu Pro")
        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        plan = [
            TerraformInitStep(tfhelper),
            EnableUbuntuProApplicationStep(
                self.deployment.get_client(),
                tfhelper,
                jhelper,
                self.manifest,
                self.token,
                self.deployment.infrastructure_model,
            ),
        ]

        run_plan(plan, console)

        click.echo(
            "Please check minimum hardware requirements for support:\n\n"
            "    https://microstack.run/docs/enterprise-reqs\n"
        )
        click.echo("Ubuntu Pro enabled.")

    def run_disable_plans(self):
        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        plan = [
            TerraformInitStep(tfhelper),
            DisableUbuntuProApplicationStep(
                self.deployment.get_client(),
                tfhelper,
                self.manifest,
            ),
        ]

        run_plan(plan, console)
        click.echo("Ubuntu Pro disabled.")

    @click.command()
    @click.option(
        "-t",
        "--token",
        help="Ubuntu Pro token to use for subscription attachment",
        prompt=True,
    )
    def enable_plugin(self, token: str) -> None:
        """Enable Ubuntu Pro across deployment.

        Minimum hardware requirements for support:

        https://microstack.run/docs/enterprise-reqs
        """
        self.token = token
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        super().disable_plugin()
