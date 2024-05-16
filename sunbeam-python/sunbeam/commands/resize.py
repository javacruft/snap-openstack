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

import click
from rich.console import Console

from sunbeam.clusterd.client import Client
from sunbeam.commands.microceph import (
    DeployMicrocephApplicationStep,
    SetCephMgrPoolSizeStep,
)
from sunbeam.commands.openstack import DeployControlPlaneStep
from sunbeam.commands.terraform import TerraformInitStep
from sunbeam.jobs.common import click_option_topology, run_plan
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import Manifest

LOG = logging.getLogger(__name__)
console = Console()


@click.command()
@click_option_topology
@click.option(
    "-f", "--force", help="Force resizing to incompatible topology.", is_flag=True
)
@click.pass_context
def resize(ctx: click.Context, topology: str, force: bool = False) -> None:
    """Expand the control plane to fit available nodes."""
    deployment: Deployment = ctx.obj
    client: Client = deployment.get_client()
    manifest = Manifest.load_latest_from_clusterdb(deployment, include_defaults=True)

    openstack_tfhelper = deployment.get_tfhelper("openstack-plan")
    microceph_tfhelper = deployment.get_tfhelper("microceph-plan")
    jhelper = JujuHelper(deployment.get_connected_controller())

    storage_nodes = client.cluster.list_nodes_by_role("storage")

    plan = []
    if len(storage_nodes):
        # Change default-pool-size based on number of storage nodes
        plan.extend(
            [
                TerraformInitStep(microceph_tfhelper),
                DeployMicrocephApplicationStep(
                    client,
                    microceph_tfhelper,
                    jhelper,
                    manifest,
                    deployment.infrastructure_model,
                    refresh=True,
                ),
                SetCephMgrPoolSizeStep(
                    client,
                    jhelper,
                    deployment.infrastructure_model,
                ),
            ]
        )

    plan.extend(
        [
            TerraformInitStep(openstack_tfhelper),
            DeployControlPlaneStep(
                client,
                openstack_tfhelper,
                jhelper,
                manifest,
                topology,
                "auto",
                deployment.infrastructure_model,
                force=force,
            ),
        ]
    )

    run_plan(plan, console)

    click.echo("Resize complete.")
