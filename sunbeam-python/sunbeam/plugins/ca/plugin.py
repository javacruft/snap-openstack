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

import json
import logging
from typing import List, Optional

import click
import yaml
from packaging.version import Version
from rich.console import Console
from rich.status import Status
from rich.table import Table

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
)
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.jobs import questions
from sunbeam.jobs.common import (
    FORMAT_TABLE,
    FORMAT_YAML,
    BaseStep,
    Result,
    ResultType,
    read_config,
    run_plan,
    str_presenter,
)
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import (
    ActionFailedException,
    JujuHelper,
    LeaderNotFoundException,
    run_sync,
)
from sunbeam.jobs.manifest import CharmManifest, SoftwareConfig
from sunbeam.plugins.interface.utils import (
    encode_base64_as_string,
    get_subject_from_csr,
    is_certificate_valid,
    validate_ca_certificate,
    validate_ca_chain,
)
from sunbeam.plugins.interface.v1.openstack import TerraformPlanLocation
from sunbeam.plugins.interface.v1.tls import TlsPluginGroup

CERTIFICATE_PLUGIN_KEY = "TlsProvider"
CA_APP_NAME = "manual-tls-certificates"
LOG = logging.getLogger(__name__)
console = Console()


def certificate_questions(unit: str, subject: str):
    return {
        "certificate": questions.PromptQuestion(
            f"Base64 encoded Certificate for {unit} CSR Unique ID: {subject}",
        ),
    }


def get_outstanding_certificate_requests(
    app: str, model: str, jhelper: JujuHelper
) -> dict:
    """Get outstanding certificate requests from manual-tls-certificate operator.

    Returns the result from the action get-outstanding-certificate-requests
    Raises LeaderNotFoundException, ActionFailedException.
    """
    action_cmd = "get-outstanding-certificate-requests"
    unit = run_sync(jhelper.get_leader_unit(app, model))
    action_result = run_sync(jhelper.run_action(unit, model, action_cmd))
    return action_result


class ConfigureCAStep(BaseStep):
    """Configure CA certificates"""

    _CONFIG = "PluginCACertificatesConfig"

    def __init__(
        self,
        client: Client,
        jhelper: JujuHelper,
        ca_cert: str,
        ca_chain: str,
        deployment_preseed: dict | None = None,
    ):
        super().__init__("Configure CA certs", "Configuring CA certificates")
        self.client = client
        self.jhelper = jhelper
        self.ca_cert = ca_cert
        self.ca_chain = ca_chain
        self.preseed = deployment_preseed or {}
        self.app = CA_APP_NAME
        self.model = OPENSTACK_MODEL
        self.process_certs = {}

    def has_prompts(self) -> bool:
        return True

    def prompt(self, console: Optional[Console] = None) -> None:
        """Prompt the user for certificates.

        Prompts the user for required information for cert configuration.

        :param console: the console to prompt on
        :type console: rich.console.Console (Optional)
        """
        action_cmd = "get-outstanding-certificate-requests"
        try:
            action_result = get_outstanding_certificate_requests(
                self.app, self.model, self.jhelper
            )
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))
        except ActionFailedException as e:
            LOG.debug(f"Running action {action_cmd} failed")
            return Result(ResultType.FAILED, str(e))

        LOG.debug(f"Result from action {action_cmd}: {action_result}")
        if action_result.get("return-code", 0) > 1:
            raise click.ClickException(
                "Unable to get outstanding certificate requests from CA"
            )

        certs_to_process = json.loads(action_result.get("result"))
        if not certs_to_process:
            LOG.debug("No outstanding certificates to process")
            return

        variables = questions.load_answers(self.client, self._CONFIG)
        variables.setdefault("certificates", {})
        self.preseed.setdefault("certificates", {})

        for record in certs_to_process:
            unit_name = record.get("unit_name")
            csr = record.get("csr")
            app = record.get("application_name")
            relation_id = record.get("relation_id")

            # Each unit can have multiple CSRs
            subject = get_subject_from_csr(csr)
            if not subject:
                raise click.ClickException(f"Not a valid CSR for unit {unit_name}")

            cert_questions = certificate_questions(unit_name, subject)
            certificates_bank = questions.QuestionBank(
                questions=cert_questions,
                console=console,
                preseed=self.preseed.get("certificates").get(subject),
                previous_answers=variables.get("certificates").get(subject),
            )
            cert = certificates_bank.certificate.ask()
            if not is_certificate_valid(cert):
                raise click.ClickException("Not a valid certificate")

            self.process_certs[subject] = {
                "app": app,
                "unit": unit_name,
                "relation_id": relation_id,
                "csr": csr,
                "certificate": cert,
            }
            variables["certificates"].setdefault(subject, {})
            variables["certificates"][subject]["certificate"] = cert

        questions.write_answers(self.client, self._CONFIG, variables)

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Run configure steps."""
        action_cmd = "provide-certificate"
        try:
            unit = run_sync(self.jhelper.get_leader_unit(self.app, self.model))
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))

        LOG.debug(f"Process certs: {self.process_certs}")
        for subject, request in self.process_certs.items():
            csr = request.get("csr")
            csr = encode_base64_as_string(csr)
            if not csr:
                return Result(ResultType.FAILED)

            action_params = {
                "relation-id": request.get("relation_id"),
                "certificate": request.get("certificate"),
                "ca-chain": self.ca_chain,
                "ca-certificate": self.ca_cert,
                "certificate-signing-request": str(csr),
                "unit-name": request.get("unit"),
            }

            LOG.debug(f"Running action {action_cmd} with params {action_params}")
            try:
                action_result = run_sync(
                    self.jhelper.run_action(unit, self.model, action_cmd, action_params)
                )
            except ActionFailedException as e:
                LOG.debug(f"Running action {action_cmd} on {unit} failed")
                return Result(ResultType.FAILED, str(e))

            LOG.debug(f"Result from action {action_cmd}: {action_result}")
            if action_result.get("return-code", 0) > 1:
                return Result(
                    ResultType.FAILED, f"Action {action_cmd} on {unit} returned error"
                )

        return Result(ResultType.COMPLETED)


class CaTlsPlugin(TlsPluginGroup):
    version = Version("0.0.1")

    def __init__(self, deployment: Deployment) -> None:
        super().__init__(
            "ca",
            deployment,
            tf_plan_location=TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO,
        )
        self.endpoints = []

    def manifest_defaults(self) -> SoftwareConfig:
        """Plugin software configuration"""
        return SoftwareConfig(
            charms={"manual-tls-certificates": CharmManifest(channel="latest/stable")}
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan: {
                "charms": {
                    "manual-tls-certificates": {
                        "channel": "manual-tls-certificates-channel",
                        "revision": "manual-tls-certificates-revision",
                        "config": "manual-tls-certificates-config",
                    }
                }
            }
        }

    def preseed_questions_content(self) -> list:
        """Generate preseed manifest content."""
        certificate_question_bank = questions.QuestionBank(
            questions=certificate_questions("unit", "subject"),
            console=console,
            previous_answers={},
        )
        content = questions.show_questions(
            certificate_question_bank,
            section="certificates",
            subsection="<CSR x500UniqueIdentifier>",
            section_description="TLS Certificates",
            comment_out=True,
        )
        return content

    @click.command()
    @click.option(
        "--endpoint",
        "endpoints",
        multiple=True,
        default=["public"],
        type=click.Choice(["public", "internal"], case_sensitive=False),
        help="Specify endpoints to apply tls.",
    )
    @click.option(
        "--ca-chain",
        required=True,
        type=str,
        callback=validate_ca_chain,
        help="Base64 encoded CA Chain certificate",
    )
    @click.option(
        "--ca",
        required=True,
        type=str,
        callback=validate_ca_certificate,
        help="Base64 encoded CA certificate",
    )
    def enable_plugin(self, ca: str, ca_chain: str, endpoints: List[str]):
        self.ca = ca
        self.ca_chain = ca_chain
        self.endpoints = endpoints
        super().enable_plugin()

    @click.command()
    def disable_plugin(self):
        super().disable_plugin()
        console.print("CA plugin disabled")

    def set_application_names(self) -> list:
        """Application names handled by the terraform plan."""
        return ["manual-tls-certificates"]

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        tfvars = {"traefik-to-tls-provider": CA_APP_NAME}
        if "public" in self.endpoints:
            tfvars.update({"enable-tls-for-public-endpoint": True})
        if "internal" in self.endpoints:
            tfvars.update({"enable-tls-for-internal-endpoint": True})

        return tfvars

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        tfvars = {"traefik-to-tls-provider": None}
        if "public" in self.endpoints:
            tfvars.update({"enable-tls-for-public-endpoint": False})
        if "internal" in self.endpoints:
            tfvars.update({"enable-tls-for-internal-endpoint": False})

        return tfvars

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    @click.group()
    def tls_group(self) -> None:
        """Manage TLS."""

    @click.group()
    def ca_group(self) -> None:
        """Manage CA."""

    @click.command()
    @click.option(
        "--format",
        type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
        default=FORMAT_TABLE,
        help="Output format",
    )
    def list_outstanding_csrs(self, format: str) -> None:
        """List outstanding CSRs"""
        app = CA_APP_NAME
        model = OPENSTACK_MODEL
        action_cmd = "get-outstanding-certificate-requests"
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        try:
            action_result = get_outstanding_certificate_requests(app, model, jhelper)
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader to print CSRs")
            raise click.ClickException(str(e))
        except ActionFailedException as e:
            LOG.debug(f"Running action {action_cmd} failed")
            raise click.ClickException(str(e))

        LOG.debug(f"Result from action {action_cmd}: {action_result}")
        if action_result.get("return-code", 0) > 1:
            raise click.ClickException(
                "Unable to get outstanding certificate requests from CA"
            )

        certs_to_process = json.loads(action_result.get("result")) or {}
        csrs = {
            unit: csr
            for record in certs_to_process
            if (unit := record.get("unit_name")) and (csr := record.get("csr"))
        }

        if format == FORMAT_TABLE:
            table = Table()
            table.add_column("Unit name")
            table.add_column("CSR")
            for unit, csr in csrs.items():
                table.add_row(unit, csr)
            console.print(table)
        elif format == FORMAT_YAML:
            yaml.add_representer(str, str_presenter)
            console.print(yaml.dump(csrs))

    @click.command()
    def configure(self) -> None:
        """Configure Unit certs."""
        client = self.deployment.get_client()
        try:
            config = read_config(client, CERTIFICATE_PLUGIN_KEY)
        except ConfigItemNotFoundException:
            config = {}
        self.ca = config.get("ca")
        self.ca_chain = config.get("chain")

        jhelper = JujuHelper(self.deployment.get_connected_controller())
        plan = [
            ConfigureCAStep(
                client,
                jhelper,
                self.ca,
                self.ca_chain,
            )
        ]
        run_plan(plan, console)
        click.echo("CA certs configured")

    def commands(self) -> dict:
        try:
            enabled = self.enabled
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for plugin status, is cloud bootstrapped ?",
                exc_info=True,
            )
            enabled = False

        commands = super().commands()
        commands.update(
            {
                "enable.tls": [{"name": self.name, "command": self.enable_plugin}],
                "disable.tls": [{"name": self.name, "command": self.disable_plugin}],
            }
        )
        if enabled:
            commands.update(
                {
                    "init": [{"name": self.group, "command": self.tls_group}],
                    "init.tls": [{"name": self.name, "command": self.ca_group}],
                    "init.tls.ca": [
                        {"name": "unit_certs", "command": self.configure},
                        {
                            "name": "list_outstanding_csrs",
                            "command": self.list_outstanding_csrs,
                        },
                    ],
                }
            )

        return commands
