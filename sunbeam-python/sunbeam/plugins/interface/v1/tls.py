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

import logging
from typing import Optional

import click
from packaging.version import Version
from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.jobs.common import (
    BaseStep,
    Result,
    ResultType,
    read_config,
    run_plan,
    update_config,
)
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import (
    ActionFailedException,
    JujuHelper,
    LeaderNotFoundException,
    run_sync,
)
from sunbeam.plugins.interface.v1.openstack import (
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)

CERTIFICATE_PLUGIN_KEY = "TlsProvider"
LOG = logging.getLogger(__name__)
console = Console()


class TlsPluginGroup(OpenStackControlPlanePlugin):
    version = Version("0.0.1")

    def __init__(
        self, name: str, deployment: Deployment, tf_plan_location: TerraformPlanLocation
    ) -> None:
        super().__init__(name, deployment, tf_plan_location)
        self.group = "tls"
        self.ca = None
        self.ca_chain = None

    @click.group()
    def enable_tls(self) -> None:
        """Enable TLS group."""

    @click.group()
    def disable_tls(self) -> None:
        """Disable TLS group."""

    def commands(self) -> dict:
        return {
            "enable": [{"name": self.group, "command": self.enable_tls}],
            "disable": [{"name": self.group, "command": self.disable_tls}],
        }

    def pre_enable(self):
        """Handler to perform tasks before enabling the plugin."""
        super().pre_enable()
        try:
            config = read_config(self.deployment.get_client(), CERTIFICATE_PLUGIN_KEY)
        except ConfigItemNotFoundException:
            config = {}

        provider = config.get("provider")
        if provider and provider != self.name:
            raise Exception(f"Certificate provider already set to {provider!r}")

    def post_enable(self) -> None:
        """Handler to perform tasks after the plugin is enabled."""
        super().post_enable()
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        plan = [
            AddCACertsToKeystoneStep(jhelper, self.plugin_key, self.ca, self.ca_chain)
        ]
        run_plan(plan, console)

        config = {
            "provider": self.name,
            "ca": self.ca,
            "chain": self.ca_chain,
            "endpoints": self.endpoints,
        }
        update_config(self.deployment.get_client(), CERTIFICATE_PLUGIN_KEY, config)

    def post_disable(self) -> None:
        """Handler to perform tasks after the plugin is disabled."""
        super().post_disable()
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        plan = [RemoveCACertsFromKeystoneStep(jhelper, self.plugin_key)]
        run_plan(plan, console)

        config = {}
        update_config(self.deployment.get_client(), CERTIFICATE_PLUGIN_KEY, config)


class AddCACertsToKeystoneStep(BaseStep):
    """Transfer CA certificates"""

    def __init__(
        self,
        jhelper: JujuHelper,
        name: str,
        ca_cert: str,
        ca_chain: str,
    ):
        super().__init__(
            "Transfer CA certs to keystone", "Transferring CA certificates to keystone"
        )
        self.jhelper = jhelper
        self.name = name.lower()
        self.ca_cert = ca_cert
        self.ca_chain = ca_chain
        self.app = "keystone"
        self.model = OPENSTACK_MODEL

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        action_cmd = "list-ca-certs"
        try:
            unit = run_sync(self.jhelper.get_leader_unit(self.app, self.model))
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))

        try:
            action_result = run_sync(
                self.jhelper.run_action(unit, self.model, action_cmd)
            )
        except ActionFailedException as e:
            LOG.debug(f"Running action {action_cmd} on {unit} failed")
            return Result(ResultType.FAILED, str(e))

        LOG.debug(f"Result from action {action_cmd}: {action_result}")
        if action_result.get("return-code", 0) > 1:
            return Result(
                ResultType.FAILED, f"Action {action_cmd} on {unit} returned error"
            )

        action_result.pop("return-code")
        ca_list = action_result
        if self.name in ca_list:
            return Result(ResultType.SKIPPED)

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Run keystone add-ca-certs action."""
        action_cmd = "add-ca-certs"
        try:
            unit = run_sync(self.jhelper.get_leader_unit(self.app, self.model))
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))

        action_params = {
            "name": self.name,
            "ca": self.ca_cert,
            "chain": self.ca_chain,
        }

        try:
            LOG.debug(f"Running action {action_cmd} with params {action_params}")
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


class RemoveCACertsFromKeystoneStep(BaseStep):
    """Remove CA certificates"""

    def __init__(
        self,
        jhelper: JujuHelper,
        name: str,
    ):
        super().__init__(
            "Remove CA certs from keystone", "Removing CA certificates from keystone"
        )
        self.jhelper = jhelper
        self.name = name.lower()
        self.app = "keystone"
        self.model = OPENSTACK_MODEL

    def is_skip(self, status: Optional[Status] = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        action_cmd = "list-ca-certs"
        try:
            unit = run_sync(self.jhelper.get_leader_unit(self.app, self.model))
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))

        try:
            action_result = run_sync(
                self.jhelper.run_action(unit, self.model, action_cmd)
            )
        except ActionFailedException as e:
            LOG.debug(f"Running action {action_cmd} on {unit} failed")
            return Result(ResultType.FAILED, str(e))

        LOG.debug(f"Result from action {action_cmd}: {action_result}")
        if action_result.get("return-code", 0) > 1:
            return Result(
                ResultType.FAILED, f"Action {action_cmd} on {unit} returned error"
            )

        action_result.pop("return-code")
        ca_list = action_result
        if self.name not in ca_list:
            return Result(ResultType.SKIPPED)

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Run keystone add-ca-certs action."""
        action_cmd = "remove-ca-certs"
        try:
            unit = run_sync(self.jhelper.get_leader_unit(self.app, self.model))
        except LeaderNotFoundException as e:
            LOG.debug(f"Unable to get {self.app} leader")
            return Result(ResultType.FAILED, str(e))

        action_params = {"name": self.name}
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
