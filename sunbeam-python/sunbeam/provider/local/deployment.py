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

import pydantic
import snaphelpers
from rich.console import Console

from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
)
from sunbeam.commands.clusterd import CLUSTERD_PORT
from sunbeam.commands.configure import (
    CLOUD_CONFIG_SECTION,
    ext_net_questions,
    ext_net_questions_local_only,
    user_questions,
)
from sunbeam.commands.juju import BOOTSTRAP_CONFIG_KEY, bootstrap_questions
from sunbeam.commands.microceph import microceph_questions
from sunbeam.commands.microk8s import (
    MICROK8S_ADDONS_CONFIG_KEY,
    microk8s_addons_questions,
)
from sunbeam.commands.proxy import proxy_questions
from sunbeam.jobs.deployment import PROXY_CONFIG_KEY, Deployment
from sunbeam.jobs.juju import JujuAccount, JujuAccountNotFound, JujuController
from sunbeam.jobs.plugin import PluginManager
from sunbeam.jobs.questions import QuestionBank, load_answers, show_questions

LOG = logging.getLogger(__name__)
LOCAL_TYPE = "local"


class LocalDeployment(Deployment):
    name: str = "local"
    url: str = "local"
    type: str = LOCAL_TYPE
    _client: Client | None = pydantic.PrivateAttr(default=None)

    def __init__(self, **data):
        super().__init__(**data)
        if self.juju_account is None:
            self.juju_account = self._load_juju_account()
        if self.juju_controller is None:
            self.juju_controller = self._load_juju_controller()

    def _load_juju_account(self) -> JujuAccount | None:
        try:
            juju_account = JujuAccount.load(snaphelpers.Snap().paths.user_data)
            LOG.debug(f"Local account found: {juju_account.user}")
            return juju_account
        except JujuAccountNotFound:
            LOG.debug("No juju account found", exc_info=True)
            return None

    def _load_juju_controller(self) -> JujuController | None:
        try:
            return JujuController.load(self.get_client())
        except ConfigItemNotFoundException:
            LOG.debug("No juju controller found", exc_info=True)
            return None
        except ClusterServiceUnavailableException:
            LOG.debug("Clusterd service unavailable", exc_info=True)
            return None

    def reload_juju_credentials(self):
        self.juju_account = self._load_juju_account()
        self.juju_controller = self._load_juju_controller()

    @property
    def infrastructure_model(self) -> str:
        """Return the infrastructure model name."""
        return "controller"

    def get_client(self) -> Client:
        """Return a client for the deployment."""
        if self._client is None:
            self._client = Client.from_socket()
        return self._client

    def get_clusterd_http_address(self) -> str:
        """Return the address of the clusterd server."""
        local_ip = utils.get_local_ip_by_default_route()
        address = f"https://{local_ip}:{CLUSTERD_PORT}"
        return address

    def generate_preseed(self, console: Console) -> str:
        """Generate preseed for deployment."""
        fqdn = utils.get_fqdn()
        client = self.get_client()
        preseed_content = ["deployment:"]
        try:
            variables = load_answers(client, PROXY_CONFIG_KEY)
        except ClusterServiceUnavailableException:
            default_proxy_settings = self.get_default_proxy_settings()
            default_proxy_settings = {
                k.lower(): v for k, v in default_proxy_settings.items() if v
            }
            variables = {"proxy": {}}

            variables["proxy"]["proxy_required"] = (
                True if default_proxy_settings else False
            )
            variables["proxy"].update(default_proxy_settings)
        proxy_bank = QuestionBank(
            questions=proxy_questions(),
            console=console,
            previous_answers=variables.get("proxy", {}),
        )
        preseed_content.extend(show_questions(proxy_bank, section="proxy"))
        try:
            variables = load_answers(client, BOOTSTRAP_CONFIG_KEY)
        except ClusterServiceUnavailableException:
            variables = {}
        bootstrap_bank = QuestionBank(
            questions=bootstrap_questions(),
            console=console,
            previous_answers=variables.get("bootstrap", {}),
        )
        preseed_content.extend(show_questions(bootstrap_bank, section="bootstrap"))
        try:
            variables = load_answers(client, MICROK8S_ADDONS_CONFIG_KEY)
        except ClusterServiceUnavailableException:
            variables = {}
        microk8s_addons_bank = QuestionBank(
            questions=microk8s_addons_questions(),
            console=console,
            previous_answers=variables.get("addons", {}),
        )
        preseed_content.extend(show_questions(microk8s_addons_bank, section="addons"))

        try:
            variables = load_answers(client, CLOUD_CONFIG_SECTION)
        except ClusterServiceUnavailableException:
            variables = {}
        user_bank = QuestionBank(
            questions=user_questions(),
            console=console,
            previous_answers=variables.get("user"),
        )
        preseed_content.extend(show_questions(user_bank, section="user"))
        ext_net_bank_local = QuestionBank(
            questions=ext_net_questions_local_only(),
            console=console,
            previous_answers=variables.get("external_network"),
        )
        preseed_content.extend(
            show_questions(
                ext_net_bank_local,
                section="external_network",
                section_description="Local Access",
            )
        )
        ext_net_bank_remote = QuestionBank(
            questions=ext_net_questions(),
            console=console,
            previous_answers=variables.get("external_network"),
        )
        preseed_content.extend(
            show_questions(
                ext_net_bank_remote,
                section="external_network",
                section_description="Remote Access",
                comment_out=True,
            )
        )
        microceph_content = []
        for name, disks in variables.get("microceph_config", {fqdn: ""}).items():
            microceph_config_bank = QuestionBank(
                questions=microceph_questions(),
                console=console,
                previous_answers=disks,
            )
            lines = show_questions(
                microceph_config_bank,
                section="microceph_config",
                subsection=name,
                section_description="MicroCeph config",
            )
            # if there's more than one microceph,
            # don't rewrite the section and section description
            if len(microceph_content) < 2:
                microceph_content.extend(lines)
            else:
                microceph_content.extend(lines[2:])
        preseed_content.extend(microceph_content)

        preseed_content.extend(PluginManager().get_preseed_questions_content(self))

        preseed_content_final = "\n".join(preseed_content)
        return preseed_content_final

    def get_default_proxy_settings(self) -> dict:
        """Return default proxy settings"""
        with open("/etc/environment", mode="r", encoding="utf-8") as file:
            current_env = dict(
                line.strip().split("=", 1) for line in file if "=" in line
            )

        proxy_configs = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]
        proxy = {p: v for p in proxy_configs if (v := current_env.get(p))}
        return proxy
