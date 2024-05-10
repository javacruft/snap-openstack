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

"""Observability plugin.

Plugin to deploy and manage observability, powered by COS Lite.
"""

import logging
from pathlib import Path
from typing import Optional

import click
from packaging.version import Version
from rich.console import Console
from rich.status import Status

from sunbeam.clusterd.service import ClusterServiceUnavailableException
from sunbeam.commands.juju import JujuStepHelper
from sunbeam.commands.k8s import CREDENTIAL_SUFFIX, K8SHelper
from sunbeam.commands.openstack import OPENSTACK_MODEL, PatchLoadBalancerServicesStep
from sunbeam.commands.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.jobs.common import (
    BaseStep,
    Result,
    ResultType,
    convert_proxy_to_model_configs,
    run_plan,
    update_status_background,
)
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.juju import JujuHelper, JujuWaitException, TimeoutException, run_sync
from sunbeam.jobs.manifest import (
    AddManifestStep,
    CharmManifest,
    Manifest,
    SoftwareConfig,
    TerraformManifest,
)
from sunbeam.plugins.interface.v1.base import PluginRequirement
from sunbeam.plugins.interface.v1.openstack import (
    DisableOpenStackApplicationStep,
    EnableOpenStackApplicationStep,
    OpenStackControlPlanePlugin,
    TerraformPlanLocation,
)

LOG = logging.getLogger(__name__)
console = Console()

OBSERVABILITY_MODEL = "observability"
OBSERVABILITY_DEPLOY_TIMEOUT = 1200  # 20 minutes
COS_TFPLAN = "cos-plan"
GRAFANA_AGENT_TFPLAN = "grafana-agent-plan"
COS_CONFIG_KEY = "TerraformVarsPluginObservabilityPlanCos"
GRAFANA_AGENT_CONFIG_KEY = "TerraformVarsPluginObservabilityPlanGrafanaAgent"

COS_CHANNEL = "1.0/stable"
GRAFANA_AGENT_CHANNEL = "latest/stable"
GRAFANA_AGENT_K8S_CHANNEL = "latest/stable"


class DeployObservabilityStackStep(BaseStep, JujuStepHelper):
    """Deploy Observability Stack using Terraform"""

    _CONFIG = COS_CONFIG_KEY

    def __init__(
        self,
        plugin: "ObservabilityPlugin",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Deploy Observability Stack", "Deploying Observability Stack")
        self.plugin = plugin
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.plugin.manifest
        self.client = self.plugin.deployment.get_client()
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud()

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        proxy_settings = self.plugin.deployment.get_proxy_settings()
        model_config = convert_proxy_to_model_configs(proxy_settings)
        model_config.update({"workload-storage": K8SHelper.get_default_storageclass()})
        extra_tfvars = {
            "model": self.model,
            "cloud": self.cloud,
            "credential": f"{self.cloud}{CREDENTIAL_SUFFIX}",
            "config": model_config,
        }

        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error deploying Observability Stack")
            return Result(ResultType.FAILED, str(e))

        apps = run_sync(self.jhelper.get_application_names(self.model))
        LOG.debug(f"Application monitored for readiness: {apps}")
        task = run_sync(update_status_background(self, apps, status))
        try:
            run_sync(
                self.jhelper.wait_until_active(
                    self.model,
                    apps,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug("Failed to deploy Observability Stack", exc_info=True)
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class UpdateObservabilityModelConfigStep(BaseStep, JujuStepHelper):
    """Update Observability Model config  using Terraform"""

    _CONFIG = COS_CONFIG_KEY

    def __init__(
        self,
        plugin: "ObservabilityPlugin",
        tfhelper: TerraformHelper,
    ):
        super().__init__(
            "Update Observability Model Config",
            "Updating Observability proxy related model config",
        )
        self.plugin = plugin
        self.tfhelper = tfhelper
        self.manifest = self.plugin.manifest
        self.client = self.plugin.deployment.get_client()
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud()

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        proxy_settings = self.plugin.deployment.get_proxy_settings()
        model_config = convert_proxy_to_model_configs(proxy_settings)
        model_config.update({"workload-storage": K8SHelper.get_default_storageclass()})
        extra_tfvars = {
            "model": self.model,
            "cloud": self.cloud,
            "credential": f"{self.cloud}{CREDENTIAL_SUFFIX}",
            "config": model_config,
        }

        try:
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
                tf_apply_extra_args=["-target=juju_model.cos"],
            )
        except TerraformException as e:
            LOG.exception("Error updating Observability Model config")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveSaasApplicationsStep(BaseStep):
    """Removes SAAS offers from given model.

    This is a workaround around:
    https://github.com/juju/terraform-provider-juju/issues/473
    """

    def __init__(self, jhelper: JujuHelper, model: str, offering_model: str):
        super().__init__(
            f"Purge SAAS Offers: {model}", f"Purging SAAS Offers from {model}"
        )
        self.jhelper = jhelper
        self.model = model
        self.offering_model = offering_model
        self._remote_app_to_delete = []

    def is_skip(self, status: Status | None = None) -> Result:
        model = run_sync(self.jhelper.get_model(self.model))
        remote_applications = model.remote_applications
        LOG.debug(
            "Remote applications found: %s", ", ".join(remote_applications.keys())
        )
        if not remote_applications:
            return Result(ResultType.SKIPPED, "No remote applications found")

        for name, remote_app in remote_applications.items():
            if not remote_app:
                continue
            offer = remote_app.offer_url
            LOG.debug("Processing offer: %s", offer)
            model_name = offer.split("/", 1)[1].split(".", 1)[0]
            if model_name == self.offering_model:
                self._remote_app_to_delete.append(name)

        if len(self._remote_app_to_delete) == 0:
            return Result(ResultType.SKIPPED, "No remote applications to remove")

        return Result(ResultType.COMPLETED)

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        if not self._remote_app_to_delete:
            return Result(ResultType.COMPLETED)

        model = run_sync(self.jhelper.get_model(self.model))

        for saas in self._remote_app_to_delete:
            LOG.debug("Removing remote application %s", saas)
            run_sync(model.remove_saas(saas))
        return Result(ResultType.COMPLETED)


class DeployGrafanaAgentStep(BaseStep, JujuStepHelper):
    """Deploy Grafana Agent using Terraform"""

    _CONFIG = GRAFANA_AGENT_CONFIG_KEY

    def __init__(
        self,
        plugin: "ObservabilityPlugin",
        tfhelper: TerraformHelper,
        tfhelper_cos: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Deploy Grafana Agent", "Deploy Grafana Agent")
        self.plugin = plugin
        self.tfhelper = tfhelper
        self.tfhelper_cos = tfhelper_cos
        self.jhelper = jhelper
        self.manifest = self.plugin.manifest
        self.client = self.plugin.deployment.get_client()
        self.model = self.plugin.deployment.infrastructure_model

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        cos_backend = self.tfhelper_cos.backend
        cos_backend_config = self.tfhelper_cos.backend_config()

        extra_tfvars = {
            "principal-application-model": self.model,
            "cos-state-backend": cos_backend,
            "cos-state-config": cos_backend_config,
            "principal-application": "openstack-hypervisor",
        }

        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error deploying grafana agent")
            return Result(ResultType.FAILED, str(e))

        app = "grafana-agent"
        LOG.debug(f"Application monitored for readiness: {app}")
        try:
            run_sync(
                self.jhelper.wait_application_ready(
                    app,
                    self.model,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug("Failed to deploy grafana agent", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveObservabilityStackStep(BaseStep, JujuStepHelper):
    """Remove Observability Stack using Terraform"""

    def __init__(
        self,
        plugin: "ObservabilityPlugin",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Remove Observability Stack", "Removing Observability Stack")
        self.plugin = plugin
        self.tfhelper = tfhelper
        self.manifest = self.plugin.manifest
        self.jhelper = jhelper
        self.model = OBSERVABILITY_MODEL
        self.cloud = K8SHelper.get_cloud()

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        try:
            self.tfhelper.destroy()
        except TerraformException as e:
            LOG.exception("Error destroying Observability Stack")
            return Result(ResultType.FAILED, str(e))

        try:
            run_sync(
                self.jhelper.wait_model_gone(
                    self.model,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except TimeoutException as e:
            LOG.debug("Failed to destroy Observability Stack", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class RemoveGrafanaAgentStep(BaseStep, JujuStepHelper):
    """Remove Grafana Agent using Terraform"""

    def __init__(
        self,
        plugin: "ObservabilityPlugin",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Remove Grafana Agent", "Removing Grafana Agent")
        self.plugin = plugin
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.plugin.manifest
        self.model = self.plugin.deployment.infrastructure_model

    def run(self, status: Optional[Status] = None) -> Result:
        """Execute configuration using terraform."""
        try:
            self.tfhelper.destroy()
        except TerraformException as e:
            LOG.exception("Error destroying grafana agent")
            return Result(ResultType.FAILED, str(e))

        apps = ["grafana-agent"]
        try:
            run_sync(
                self.jhelper.wait_application_gone(
                    apps,
                    self.model,
                    timeout=OBSERVABILITY_DEPLOY_TIMEOUT,
                )
            )
        except TimeoutException as e:
            LOG.debug("Failed to destroy grafana agent", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class PatchCosLoadBalancerStep(PatchLoadBalancerServicesStep):
    SERVICES = ["traefik"]
    MODEL = OBSERVABILITY_MODEL


class ObservabilityPlugin(OpenStackControlPlanePlugin):
    version = Version("0.0.1")
    requires = {PluginRequirement("telemetry")}

    def __init__(self, deployment: Deployment) -> None:
        super().__init__(
            "observability", deployment, TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO
        )
        self.tfplan_cos = COS_TFPLAN
        self.tfplan_cos_dir = "deploy-cos"
        self.tfplan_grafana_agent = GRAFANA_AGENT_TFPLAN
        self.tfplan_grafana_agent_dir = "deploy-grafana-agent"
        self.tfplan_grafana_agent_k8s_dir = "deploy-grafana-agent-k8s"

    @property
    def manifest(self) -> Manifest:
        if self._manifest:
            return self._manifest

        self._manifest = self.deployment.get_manifest()
        return self._manifest

    def manifest_defaults(self) -> SoftwareConfig:
        """Plugin software configuration"""
        return SoftwareConfig(
            charms={
                "cos-traefik-k8s": CharmManifest(channel=COS_CHANNEL),
                "alertmanager-k8s": CharmManifest(channel=COS_CHANNEL),
                "grafana-k8s": CharmManifest(channel=COS_CHANNEL),
                "catalogue-k8s": CharmManifest(channel=COS_CHANNEL),
                "prometheus-k8s": CharmManifest(channel=COS_CHANNEL),
                "loki-k8s": CharmManifest(channel=COS_CHANNEL),
                "grafana-agent": CharmManifest(channel=GRAFANA_AGENT_CHANNEL),
                "grafana-agent-k8s": CharmManifest(channel=GRAFANA_AGENT_K8S_CHANNEL),
            },
            terraform={
                self.tfplan_cos: TerraformManifest(
                    source=Path(__file__).parent / "etc" / self.tfplan_cos_dir
                ),
                self.tfplan_grafana_agent: TerraformManifest(
                    source=Path(__file__).parent
                    / "etc"  # noqa: W503
                    / self.tfplan_grafana_agent_dir  # noqa: W503
                ),
            },
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan_cos: {
                "charms": {
                    "cos-traefik-k8s": {
                        "channel": "traefik-channel",
                        "revision": "traefik-revision",
                        "config": "traefik-config",
                    },
                    "alertmanager-k8s": {
                        "channel": "alertmanager-channel",
                        "revision": "alertmanager-revision",
                        "config": "alertmanager-config",
                    },
                    "grafana-k8s": {
                        "channel": "grafana-channel",
                        "revision": "grafana-revision",
                        "config": "grafana-config",
                    },
                    "catalogue-k8s": {
                        "channel": "catalogue-channel",
                        "revision": "catalogue-revision",
                        "config": "catalogue-config",
                    },
                    "prometheus-k8s": {
                        "channel": "prometheus-channel",
                        "revision": "prometheus-revision",
                        "config": "prometheus-config",
                    },
                    "loki-k8s": {
                        "channel": "loki-channel",
                        "revision": "loki-revision",
                        "config": "loki-config",
                    },
                }
            },
            self.tfplan_grafana_agent: {
                "charms": {
                    "grafana-agent": {
                        "channel": "grafana-agent-channel",
                        "revision": "grafana-agent-revision",
                        "config": "grafana-agent-config",
                    }
                }
            },
            self.tfplan: {
                "charms": {
                    "grafana-agent-k8s": {
                        "channel": "grafana-agent-channel",
                        "revision": "grafana-agent-revision",
                        "config": "grafana-agent-config",
                    }
                }
            },
        }

    def update_proxy_model_configs(self) -> None:
        try:
            if not self.enabled:
                LOG.debug("Observability plugin is not enabled, nothing to do")
                return
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for plugin status, is cloud bootstrapped ?",
                exc_info=True,
            )
            return

        plan = [
            TerraformInitStep(self.deployment.get_tfhelper(self.tfplan_cos)),
            UpdateObservabilityModelConfigStep(
                self, self.deployment.get_tfhelper(self.tfplan_cos)
            ),
        ]
        run_plan(plan, console)

    def set_application_names(self) -> list:
        """Application names handled by the main terraform plan."""
        # main plan only handles grafana-agent-k8s, named grafana-agent
        return ["grafana-agent"]

    def set_tfvars_on_enable(self) -> dict:
        """Set terraform variables to enable the application."""
        tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
        output = tfhelper_cos.output()
        return {
            "enable-observability": True,
            "grafana-dashboard-offer-url": output["grafana-dashboard-offer-url"],
            "logging-offer-url": output["loki-logging-offer-url"],
            "receive-remote-write-offer-url": output[
                "prometheus-receive-remote-write-offer-url"
            ],
        }

    def set_tfvars_on_disable(self) -> dict:
        """Set terraform variables to disable the application."""
        return {
            "enable-observability": False,
            "grafana-dashboard-offer-url": None,
            "logging-offer-url": None,
            "receive-remote-write-offer-url": None,
        }

    def set_tfvars_on_resize(self) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def run_enable_plans(self):
        jhelper = JujuHelper(self.deployment.get_connected_controller())

        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        client = self.deployment.get_client()
        plan = []
        if self.user_manifest:
            plan.append(AddManifestStep(client, self.user_manifest))

        cos_plan = [
            TerraformInitStep(tfhelper_cos),
            DeployObservabilityStackStep(self, tfhelper_cos, jhelper),
            PatchCosLoadBalancerStep(client),
        ]

        grafana_agent_k8s_plan = [
            TerraformInitStep(tfhelper),
            EnableOpenStackApplicationStep(tfhelper, jhelper, self),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            DeployGrafanaAgentStep(self, tfhelper_grafana_agent, tfhelper_cos, jhelper),
        ]

        run_plan(plan, console)
        run_plan(cos_plan, console)
        run_plan(grafana_agent_k8s_plan, console)
        run_plan(grafana_agent_plan, console)

        click.echo("Observability enabled.")

    def run_disable_plans(self):
        jhelper = JujuHelper(self.deployment.get_connected_controller())
        tfhelper = self.deployment.get_tfhelper(self.tfplan)
        tfhelper_cos = self.deployment.get_tfhelper(self.tfplan_cos)
        tfhelper_grafana_agent = self.deployment.get_tfhelper(self.tfplan_grafana_agent)

        agent_grafana_k8s_plan = [
            TerraformInitStep(tfhelper),
            DisableOpenStackApplicationStep(tfhelper, jhelper, self),
            RemoveSaasApplicationsStep(jhelper, OPENSTACK_MODEL, OBSERVABILITY_MODEL),
        ]

        grafana_agent_plan = [
            TerraformInitStep(tfhelper_grafana_agent),
            RemoveGrafanaAgentStep(self, tfhelper_grafana_agent, jhelper),
            RemoveSaasApplicationsStep(
                jhelper, self.deployment.infrastructure_model, OBSERVABILITY_MODEL
            ),
        ]

        cos_plan = [
            TerraformInitStep(tfhelper_cos),
            RemoveObservabilityStackStep(self, tfhelper_cos, jhelper),
        ]

        run_plan(agent_grafana_k8s_plan, console)
        run_plan(grafana_agent_plan, console)
        run_plan(cos_plan, console)
        click.echo("Observability disabled.")

    @click.command()
    def enable_plugin(self) -> None:
        """Enable Observability."""
        super().enable_plugin()

    @click.command()
    def disable_plugin(self) -> None:
        """Disable  Observability."""
        super().disable_plugin()

    @click.group()
    def observability_group(self):
        """Manage Observability."""

    @click.command()
    def dashboard_url(self) -> None:
        """Retrieve COS Dashboard URL."""
        jhelper = JujuHelper(self.deployment.get_connected_controller())

        with console.status("Retrieving dashboard URL from Grafana service ... "):
            # Retrieve config from juju actions
            model = OBSERVABILITY_MODEL
            app = "grafana"
            action_cmd = "get-admin-password"
            unit = run_sync(jhelper.get_leader_unit(app, model))
            if not unit:
                _message = f"Unable to get {app} leader"
                raise click.ClickException(_message)

            action_result = run_sync(jhelper.run_action(unit, model, action_cmd))

            if action_result.get("return-code", 0) > 1:
                _message = "Unable to retrieve URL from Grafana service"
                raise click.ClickException(_message)

            url = action_result.get("url")
            if url:
                console.print(url)
            else:
                _message = "No URL provided by Grafana service"
                raise click.ClickException(_message)

    def commands(self) -> dict:
        """Dict of clickgroup along with commands."""
        commands = super().commands()
        try:
            enabled = self.enabled
        except ClusterServiceUnavailableException:
            LOG.debug(
                "Failed to query for plugin status, is cloud bootstrapped ?",
                exc_info=True,
            )
            enabled = False

        if enabled:
            commands.update(
                {
                    "init": [
                        {"name": "observability", "command": self.observability_group}
                    ],
                    "init.observability": [
                        {"name": "dashboard-url", "command": self.dashboard_url}
                    ],
                }
            )
        return commands
