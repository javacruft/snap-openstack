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

import logging
import secrets
import string

import click
from rich.console import Console

from sunbeam.commands.juju import JujuLoginStep
from sunbeam.jobs.checks import VerifyBootstrappedCheck
from sunbeam.jobs.common import run_plan, run_preflight_checks
from sunbeam.jobs.deployment import Deployment

LOG = logging.getLogger(__name__)
console = Console()


@click.command()
@click.pass_context
def juju_login(ctx: click.Context) -> None:
    """Login to the controller with current host user."""
    deployment: Deployment = ctx.obj
    client = deployment.get_client()
    preflight_checks = [VerifyBootstrappedCheck(client)]
    run_preflight_checks(preflight_checks, console)

    plan = []
    plan.append(JujuLoginStep(deployment.juju_account))

    run_plan(plan, console)

    console.print("Juju re-login complete.")


def random_string(length: int) -> str:
    """Utility function to generate secure random string."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for i in range(length))
