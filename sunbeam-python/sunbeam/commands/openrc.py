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

import logging

import click
from rich.console import Console

from sunbeam.commands.configure import retrieve_admin_credentials
from sunbeam.commands.openstack import OPENSTACK_MODEL
from sunbeam.jobs import juju
from sunbeam.jobs.checks import DaemonGroupCheck, VerifyBootstrappedCheck
from sunbeam.jobs.common import run_preflight_checks
from sunbeam.jobs.deployment import Deployment

LOG = logging.getLogger(__name__)
console = Console()


@click.command()
@click.pass_context
def openrc(ctx: click.Context) -> None:
    """Retrieve openrc for cloud admin account."""
    deployment: Deployment = ctx.obj
    client = deployment.get_client()
    preflight_checks = []
    preflight_checks.append(DaemonGroupCheck())
    preflight_checks.append(VerifyBootstrappedCheck(client))
    run_preflight_checks(preflight_checks, console)

    jhelper = juju.JujuHelper(deployment.get_connected_controller())

    with console.status("Retrieving openrc from Keystone service ... "):
        creds = retrieve_admin_credentials(jhelper, OPENSTACK_MODEL)
        console.print("# openrc for access to OpenStack")
        for param, value in creds.items():
            console.print(f"export {param}={value}")
