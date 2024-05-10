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
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from sunbeam.commands.upgrades.inter_channel import ChannelUpgradeCoordinator
from sunbeam.commands.upgrades.intra_channel import LatestInChannelCoordinator
from sunbeam.jobs.common import run_plan
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper
from sunbeam.jobs.manifest import AddManifestStep

LOG = logging.getLogger(__name__)
console = Console()


@click.command()
@click.option(
    "-c",
    "--clear-manifest",
    is_flag=True,
    default=False,
    help="Clear the manifest file.",
    type=bool,
)
@click.option(
    "-m",
    "--manifest",
    "manifest_path",
    help="Manifest file.",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--upgrade-release",
    is_flag=True,
    show_default=True,
    default=False,
    help="Upgrade OpenStack release.",
)
@click.pass_context
def refresh(
    ctx: click.Context,
    upgrade_release: bool,
    manifest_path: Optional[Path] = None,
    clear_manifest: bool = False,
) -> None:
    """Refresh deployment.

    Refresh the deployment. If --upgrade-release is supplied then charms are
    upgraded the channels aligned with this snap revision
    """
    if clear_manifest and manifest_path:
        raise click.ClickException(
            "Options manifest and clear_manifest are mutually exclusive"
        )

    deployment: Deployment = ctx.obj
    client = deployment.get_client()
    # Validate manifest file
    manifest = None
    if clear_manifest:
        run_plan([AddManifestStep(client, clear=True)], console)
    elif manifest_path:
        manifest = deployment.get_manifest(manifest_path)
        run_plan([AddManifestStep(client, manifest_path)], console)

    if not manifest:
        LOG.debug("Getting latest manifest from cluster db")
        manifest = deployment.get_manifest()

    LOG.debug(f"Manifest used for deployment - software: {manifest.software}")
    jhelper = JujuHelper(deployment.get_connected_controller())
    if upgrade_release:
        a = ChannelUpgradeCoordinator(deployment, client, jhelper, manifest)
        a.run_plan()
    else:
        a = LatestInChannelCoordinator(deployment, client, jhelper, manifest)
        a.run_plan()
    click.echo("Refresh complete.")
