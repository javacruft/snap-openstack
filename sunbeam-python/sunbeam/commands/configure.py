# Copyright (c) 2022 Canonical Ltd.
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

import ipaddress
import logging
import os
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

import sunbeam.jobs.questions
from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.commands.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.jobs.common import BaseStep, Result, ResultType, Status
from sunbeam.jobs.juju import (
    ActionFailedException,
    JujuHelper,
    LeaderNotFoundException,
    run_sync,
)

CLOUD_CONFIG_SECTION = "CloudConfig"
LOG = logging.getLogger(__name__)
console = Console()


def user_questions():
    return {
        "run_demo_setup": sunbeam.jobs.questions.ConfirmQuestion(
            "Populate OpenStack cloud with demo user, default images, flavors etc",
            default_value=True,
        ),
        "username": sunbeam.jobs.questions.PromptQuestion(
            "Username to use for access to OpenStack", default_value="demo"
        ),
        "password": sunbeam.jobs.questions.PasswordPromptQuestion(
            "Password to use for access to OpenStack",
            default_function=utils.generate_password,
            password=True,
        ),
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            "Network range to use for project network",
            default_value="192.168.122.0/24",
            validation_function=ipaddress.ip_network,
        ),
        "nameservers": sunbeam.jobs.questions.PromptQuestion(
            "List of nameservers guests should use for DNS resolution",
            default_function=lambda: " ".join(utils.get_nameservers()),
        ),
        "security_group_rules": sunbeam.jobs.questions.ConfirmQuestion(
            "Enable ping and SSH access to instances?", default_value=True
        ),
        "remote_access_location": sunbeam.jobs.questions.PromptQuestion(
            "Local or remote access to VMs",
            choices=[utils.LOCAL_ACCESS, utils.REMOTE_ACCESS],
            default_value=utils.LOCAL_ACCESS,
        ),
    }


def ext_net_questions():
    return {
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            "CIDR of network to use for external networking",
            default_value="10.20.20.0/24",
            validation_function=ipaddress.ip_network,
        ),
        "gateway": sunbeam.jobs.questions.PromptQuestion(
            "IP address of default gateway for external network",
            default_value=None,
            validation_function=ipaddress.ip_address,
        ),
        "start": sunbeam.jobs.questions.PromptQuestion(
            "Start of IP allocation range for external network",
            default_value=None,
            validation_function=ipaddress.ip_address,
        ),
        "end": sunbeam.jobs.questions.PromptQuestion(
            "End of IP allocation range for external network",
            default_value=None,
            validation_function=ipaddress.ip_address,
        ),
        "network_type": sunbeam.jobs.questions.PromptQuestion(
            "Network type for access to external network",
            choices=["flat", "vlan"],
            default_value="flat",
        ),
        "segmentation_id": sunbeam.jobs.questions.PromptQuestion(
            "VLAN ID to use for external network", default_value=0
        ),
    }


def ext_net_questions_local_only():
    return {
        "cidr": sunbeam.jobs.questions.PromptQuestion(
            (
                "CIDR of OpenStack external network - arbitrary but must not "
                "be in use"
            ),
            default_value="10.20.20.0/24",
            validation_function=ipaddress.ip_network,
        ),
        "start": sunbeam.jobs.questions.PromptQuestion(
            "Start of IP allocation range for external network",
            default_value=None,
            validation_function=ipaddress.ip_address,
        ),
        "end": sunbeam.jobs.questions.PromptQuestion(
            "End of IP allocation range for external network",
            default_value=None,
            validation_function=ipaddress.ip_address,
        ),
        "network_type": sunbeam.jobs.questions.PromptQuestion(
            "Network type for access to external network",
            choices=["flat", "vlan"],
            default_value="flat",
        ),
        "segmentation_id": sunbeam.jobs.questions.PromptQuestion(
            "VLAN ID to use for external network", default_value=0
        ),
    }


VARIABLE_DEFAULTS = {
    "user": {
        "username": "demo",
        "cidr": "192.168.122.0/24",
        "security_group_rules": True,
    },
    "external_network": {
        "cidr": "10.20.20.0/24",
        "gateway": None,
        "start": None,
        "end": None,
        "physical_network": "physnet1",
        "network_type": "flat",
        "segmentation_id": 0,
    },
}


def retrieve_admin_credentials(jhelper: JujuHelper, model: str) -> dict:
    """Retrieve cloud admin credentials.

    Retrieve cloud admin credentials from keystone and
    return as a dict suitable for use with subprocess
    commands.  Variables are prefixed with OS_.
    """
    app = "keystone"
    action_cmd = "get-admin-account"

    try:
        unit = run_sync(jhelper.get_leader_unit(app, model))
    except LeaderNotFoundException:
        raise click.ClickException(f"Unable to get {app} leader")

    try:
        action_result = run_sync(jhelper.run_action(unit, model, action_cmd))
    except ActionFailedException as e:
        LOG.debug(f"Running action {action_cmd} on {unit} failed: {str(e)}")
        raise click.ClickException("Unable to retrieve openrc from Keystone service")

    if action_result.get("return-code", 0) > 1:
        raise click.ClickException("Unable to retrieve openrc from Keystone service")

    params = {
        "OS_USERNAME": action_result.get("username"),
        "OS_PASSWORD": action_result.get("password"),
        "OS_AUTH_URL": action_result.get("public-endpoint"),
        "OS_USER_DOMAIN_NAME": action_result.get("user-domain-name"),
        "OS_PROJECT_DOMAIN_NAME": action_result.get("project-domain-name"),
        "OS_PROJECT_NAME": action_result.get("project-name"),
        "OS_AUTH_VERSION": action_result.get("api-version"),
        "OS_IDENTITY_API_VERSION": action_result.get("api-version"),
    }

    action_cmd = "list-ca-certs"
    try:
        action_result = run_sync(jhelper.run_action(unit, model, action_cmd))
    except ActionFailedException as e:
        LOG.debug(f"Running action {action_cmd} on {unit} failed: {str(e)}")
        raise click.ClickException("Unable to retrieve CA certs from Keystone service")

    if action_result.get("return-code", 0) > 1:
        raise click.ClickException("Unable to retrieve CA certs from Keystone service")

    action_result.pop("return-code")
    ca_bundle = []
    for name, certs in action_result.items():
        # certs = json.loads(certs)
        ca = certs.get("ca")
        chain = certs.get("chain")
        if ca and ca not in ca_bundle:
            ca_bundle.append(ca)
        if chain and chain not in ca_bundle:
            ca_bundle.append(chain)

    bundle = "\n".join(ca_bundle)

    if bundle:
        home = os.environ.get("SNAP_REAL_HOME")
        cafile = Path(home) / ".config" / "openstack" / "ca_bundle.pem"
        LOG.debug("Writing CA bundle to {str(cafile)}")

        cafile.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        if not cafile.exists():
            cafile.touch()
        cafile.chmod(0o660)

        with cafile.open("w") as file:
            file.write(bundle)

        params["OS_CACERT"] = str(cafile)

    return params


class SetHypervisorCharmConfigStep(BaseStep):
    """Update openstack-hypervisor charm config"""

    IPVANYNETWORK_UNSET = "0.0.0.0/0"

    def __init__(
        self, client: Client, jhelper: JujuHelper, ext_network: Path, model: str
    ):
        super().__init__(
            "Update charm config",
            "Updating openstack-hypervisor charm configuration",
        )

        # File path with external_network details in json format
        self.client = client
        self.jhelper = jhelper
        self.ext_network_file = ext_network
        self.model = model
        self.ext_network = {}
        self.charm_config = {}

    def has_prompts(self) -> bool:
        return False

    def run(self, status: Optional["Status"] = None) -> Result:
        """Run the step to completion.

        Invoked when the step is run and returns a ResultType to indicate

        :return:
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        self.ext_network = self.variables.get("external_network", {})
        self.charm_config["enable-gateway"] = str(
            self.variables["user"]["remote_access_location"] == utils.REMOTE_ACCESS
        )
        self.charm_config["external-bridge"] = "br-ex"
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            external_network = ipaddress.ip_network(
                self.variables["external_network"].get("cidr")
            )
            bridge_interface = (
                f"{self.ext_network.get('gateway')}/{external_network.prefixlen}"
            )
            self.charm_config["external-bridge-address"] = bridge_interface
        else:
            self.charm_config["external-bridge-address"] = self.IPVANYNETWORK_UNSET

        self.charm_config["physnet-name"] = self.variables["external_network"].get(
            "physical_network"
        )
        try:
            LOG.debug(
                f"Config to apply on openstack-hypervisor snap: {self.charm_config}"
            )
            run_sync(
                self.jhelper.set_application_config(
                    self.model,
                    "openstack-hypervisor",
                    self.charm_config,
                )
            )
        except Exception as e:
            LOG.exception("Error setting config for openstack-hypervisor")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class UserOpenRCStep(BaseStep):
    """Generate openrc for created cloud user."""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        auth_url: str,
        auth_version: str,
        cacert: str | None = None,
        openrc: Path | None = None,
    ):
        super().__init__(
            "Generate admin openrc", "Generating openrc for cloud admin usage"
        )
        self.client = client
        self.tfhelper = tfhelper
        self.auth_url = auth_url
        self.auth_version = auth_version
        self.cacert = cacert
        self.openrc = openrc

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if "user" not in self.variables:
            LOG.debug("Demo setup not yet done")
            return Result(ResultType.SKIPPED)
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)

    def run(self, status: Optional["Status"] = None) -> Result:
        try:
            tf_output = self.tfhelper.output(hide_output=True)
            # Mask any passwords before printing process.stdout
            self._print_openrc(tf_output)
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            LOG.exception("Error getting terraform output")
            return Result(ResultType.FAILED, str(e))

    def _print_openrc(self, tf_output: dict) -> None:
        """Print openrc to console and save to disk using provided information"""
        _openrc = f"""# openrc for {tf_output["OS_USERNAME"]}
export OS_AUTH_URL={self.auth_url}
export OS_USERNAME={tf_output["OS_USERNAME"]}
export OS_PASSWORD={tf_output["OS_PASSWORD"]}
export OS_USER_DOMAIN_NAME={tf_output["OS_USER_DOMAIN_NAME"]}
export OS_PROJECT_DOMAIN_NAME={tf_output["OS_PROJECT_DOMAIN_NAME"]}
export OS_PROJECT_NAME={tf_output["OS_PROJECT_NAME"]}
export OS_AUTH_VERSION={self.auth_version}
export OS_IDENTITY_API_VERSION={self.auth_version}"""
        if self.cacert:
            _openrc = f"{_openrc}\nexport OS_CACERT={self.cacert}"
        if self.openrc:
            message = f"Writing openrc to {self.openrc} ... "
            console.status(message)
            with self.openrc.open("w") as f_openrc:
                os.fchmod(f_openrc.fileno(), mode=0o640)
                f_openrc.write(_openrc)
            console.print(f"{message}[green]done[/green]")
        else:
            console.print(_openrc)


class UserQuestions(BaseStep):
    """Ask user configuration questions."""

    def __init__(
        self,
        client: Client,
        answer_file: Path,
        deployment_preseed: dict | None = None,
        accept_defaults: bool = False,
    ):
        super().__init__(
            "Collect cloud configuration", "Collecting cloud configuration"
        )
        self.client = client
        self.accept_defaults = accept_defaults
        self.preseed = deployment_preseed or {}
        self.answer_file = answer_file

    def has_prompts(self) -> bool:
        return True

    def prompt(self, console: Optional[Console] = None) -> None:
        """Prompt the user for basic cloud configuration.

        Prompts the user for required information for cloud configuration.

        :param console: the console to prompt on
        :type console: rich.console.Console (Optional)
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        for section in ["user", "external_network"]:
            if not self.variables.get(section):
                self.variables[section] = {}

        user_bank = sunbeam.jobs.questions.QuestionBank(
            questions=user_questions(),
            console=console,
            preseed=self.preseed.get("user"),
            previous_answers=self.variables.get("user"),
            accept_defaults=self.accept_defaults,
        )
        self.variables["user"][
            "remote_access_location"
        ] = user_bank.remote_access_location.ask()
        # External Network Configuration
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions_local_only(),
                console=console,
                preseed=self.preseed.get("external_network"),
                previous_answers=self.variables.get("external_network"),
                accept_defaults=self.accept_defaults,
            )
        else:
            ext_net_bank = sunbeam.jobs.questions.QuestionBank(
                questions=ext_net_questions(),
                console=console,
                preseed=self.preseed.get("external_network"),
                previous_answers=self.variables.get("external_network"),
                accept_defaults=self.accept_defaults,
            )
        self.variables["external_network"]["cidr"] = ext_net_bank.cidr.ask()
        external_network = ipaddress.ip_network(
            self.variables["external_network"]["cidr"]
        )
        external_network_hosts = list(external_network.hosts())
        default_gateway = self.variables["external_network"].get("gateway") or str(
            external_network_hosts[0]
        )
        if self.variables["user"]["remote_access_location"] == utils.LOCAL_ACCESS:
            self.variables["external_network"]["gateway"] = default_gateway
        else:
            self.variables["external_network"]["gateway"] = ext_net_bank.gateway.ask(
                new_default=default_gateway
            )

        default_allocation_range_start = self.variables["external_network"].get(
            "start"
        ) or str(external_network_hosts[1])
        self.variables["external_network"]["start"] = ext_net_bank.start.ask(
            new_default=default_allocation_range_start
        )
        default_allocation_range_end = self.variables["external_network"].get(
            "end"
        ) or str(external_network_hosts[-1])
        self.variables["external_network"]["end"] = ext_net_bank.end.ask(
            new_default=default_allocation_range_end
        )

        self.variables["external_network"]["physical_network"] = VARIABLE_DEFAULTS[
            "external_network"
        ]["physical_network"]

        self.variables["external_network"][
            "network_type"
        ] = ext_net_bank.network_type.ask()
        if self.variables["external_network"]["network_type"] == "vlan":
            self.variables["external_network"][
                "segmentation_id"
            ] = ext_net_bank.segmentation_id.ask()
        else:
            self.variables["external_network"]["segmentation_id"] = 0

        self.variables["user"]["run_demo_setup"] = user_bank.run_demo_setup.ask()
        if self.variables["user"]["run_demo_setup"]:
            # User configuration
            self.variables["user"]["username"] = user_bank.username.ask()
            self.variables["user"]["password"] = user_bank.password.ask()
            self.variables["user"]["cidr"] = user_bank.cidr.ask()
            self.variables["user"][
                "dns_nameservers"
            ] = user_bank.nameservers.ask().split()
            self.variables["user"][
                "security_group_rules"
            ] = user_bank.security_group_rules.ask()

        sunbeam.jobs.questions.write_answers(
            self.client, CLOUD_CONFIG_SECTION, self.variables
        )

    def run(self, status: Optional[Status] = None) -> Result:
        return Result(ResultType.COMPLETED)


class DemoSetup(BaseStep):
    """Default cloud configuration for all-in-one install."""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        answer_file: Path,
    ):
        super().__init__(
            "Create demonstration configuration",
            "Creating demonstration user, project and networking",
        )
        self.answer_file = answer_file
        self.tfhelper = tfhelper
        self.client = client

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        self.tfhelper.write_tfvars(self.variables, self.answer_file)
        try:
            self.tfhelper.apply()
            return Result(ResultType.COMPLETED)
        except TerraformException as e:
            LOG.exception("Error configuring cloud")
            return Result(ResultType.FAILED, str(e))


class TerraformDemoInitStep(TerraformInitStep):
    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
    ):
        super().__init__(tfhelper)
        self.tfhelper = tfhelper
        self.client = client

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        self.variables = sunbeam.jobs.questions.load_answers(
            self.client, CLOUD_CONFIG_SECTION
        )
        if self.variables["user"]["run_demo_setup"]:
            return Result(ResultType.COMPLETED)
        else:
            return Result(ResultType.SKIPPED)


class SetHypervisorUnitsOptionsStep(BaseStep):
    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        model: str,
        deployment_preseed: dict | None = None,
        msg: str = "Apply hypervisor settings",
        description: str = "Applying hypervisor settings",
    ):
        super().__init__(msg, description)
        self.client = client
        if isinstance(names, str):
            names = [names]
        self.names = names
        self.jhelper = jhelper
        self.model = model
        self.preseed = deployment_preseed or {}
        self.nics: dict[str, str | None] = {}

    def run(self, status: Optional[Status] = None) -> Result:
        app = "openstack-hypervisor"
        action_cmd = "set-hypervisor-local-settings"
        for name in self.names:
            self.update_status(status, f"setting hypervisor configuration for {name}")
            nic = self.nics.get(name)
            if nic is None:
                LOG.debug(f"No NIC found for hypervisor {name}, skipping.")
                continue
            node = self.client.cluster.get_node_info(name)
            self.machine_id = str(node.get("machineid"))
            unit = run_sync(
                self.jhelper.get_unit_from_machine(app, self.machine_id, self.model)
            )
            action_result = run_sync(
                self.jhelper.run_action(
                    unit.entity_id,
                    self.model,
                    action_cmd,
                    action_params={
                        "external-nic": nic,
                    },
                )
            )
            if action_result.get("return-code", 0) > 1:
                _message = "Unable to set hypervisor {name!r} configuration"
                return Result(ResultType.FAILED, _message)
        return Result(ResultType.COMPLETED)


def _sorter(name):
    if name == "deployment":
        return 0
    return 1


def _keep_cmd_params(cmd: click.Command, params: dict) -> dict:
    """Keep parameters from parent context that are in the command."""
    out_params = {}
    for param in cmd.params:
        if param.name in params:
            out_params[param.name] = params[param.name]
    return out_params


@click.group(invoke_without_command=True)
@click.pass_context
@click.option("-a", "--accept-defaults", help="Accept all defaults.", is_flag=True)
@click.option(
    "-m",
    "--manifest",
    help="Manifest file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "-o",
    "--openrc",
    help="Output file for cloud access details.",
    type=click.Path(dir_okay=False, path_type=Path),
)
def configure(
    ctx: click.Context,
    openrc: Optional[Path] = None,
    manifest: Optional[Path] = None,
    accept_defaults: bool = False,
) -> None:
    """Configure cloud with some sensible defaults."""
    if ctx.invoked_subcommand is not None:
        return
    commands = configure.commands.items()
    commands = sorted(commands, key=_sorter)
    for name, command in commands:
        LOG.debug("Running configure %r", name)
        cmd_ctx = click.Context(
            command,
            parent=ctx,
            info_name=command.name,
            allow_extra_args=True,
        )
        cmd_ctx.params = _keep_cmd_params(command, ctx.params)
        cmd_ctx.forward(command)
