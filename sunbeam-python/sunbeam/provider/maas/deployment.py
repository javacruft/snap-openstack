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

import enum
from typing import Type, TypeGuard

import pydantic

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ClusterServiceUnavailableException
from sunbeam.commands.configure import (
    CLOUD_CONFIG_SECTION,
    ext_net_questions,
    user_questions,
)
from sunbeam.commands.proxy import proxy_questions
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.plugin import PluginManager
from sunbeam.jobs.questions import Question, QuestionBank, load_answers, show_questions

MAAS_TYPE = "maas"


class Networks(enum.Enum):
    PUBLIC = "public"
    STORAGE = "storage"
    STORAGE_CLUSTER = "storage-cluster"
    INTERNAL = "internal"
    DATA = "data"
    MANAGEMENT = "management"

    @classmethod
    def values(cls) -> list[str]:
        """Return list of tag values."""
        return [tag.value for tag in cls]


class RoleTags(enum.Enum):
    CONTROL = "control"
    COMPUTE = "compute"
    STORAGE = "storage"
    JUJU_CONTROLLER = "juju-controller"

    @classmethod
    def values(cls) -> list[str]:
        """Return list of tag values."""
        return [tag.value for tag in cls]


ROLE_NETWORK_MAPPING = {
    RoleTags.CONTROL: [
        Networks.INTERNAL,
        Networks.MANAGEMENT,
        Networks.PUBLIC,
        Networks.STORAGE,
    ],
    RoleTags.COMPUTE: [
        Networks.DATA,
        Networks.INTERNAL,
        Networks.MANAGEMENT,
        Networks.STORAGE,
    ],
    RoleTags.STORAGE: [
        Networks.DATA,
        Networks.INTERNAL,
        Networks.MANAGEMENT,
        Networks.STORAGE,
        Networks.STORAGE_CLUSTER,
    ],
    RoleTags.JUJU_CONTROLLER: [
        Networks.MANAGEMENT,
    ],
}


class StorageTags(enum.Enum):
    CEPH = "ceph"

    @classmethod
    def values(cls) -> list[str]:
        """Return list of tag values."""
        return [tag.value for tag in cls]


class NicTags(enum.Enum):
    COMPUTE = "compute"

    @classmethod
    def values(cls) -> list[str]:
        """Return list of tag values."""
        return [tag.value for tag in cls]


class MaasDeployment(Deployment):
    type: str = MAAS_TYPE
    token: str
    resource_pool: str
    network_mapping: dict[str, str | None] = {}
    clusterd_address: str | None = None
    _client: Client | None = pydantic.PrivateAttr(default=None)

    @property
    def controller(self) -> str:
        """Return controller name."""
        return self.name + "-controller"

    @property
    def public_api_label(self) -> str:
        """Return public API label."""
        return self.resource_pool + "-public-api"

    @property
    def internal_api_label(self) -> str:
        """Return internal API label."""
        return self.resource_pool + "-internal-api"

    @pydantic.validator("type")
    def type_validator(cls, v: str, values: dict) -> str:
        if v != MAAS_TYPE:
            raise ValueError("Deployment type must be MAAS.")
        return v

    @classmethod
    def import_step(cls) -> Type:
        """Return a step for importing a deployment.

        This step will be used to make sure the deployment is valid.
        The step must take as constructor arguments: DeploymentsConfig, Deployment.
        The Deployment must be of the type that the step is registered for.
        """
        from sunbeam.provider.maas.commands import AddMaasDeployment

        return AddMaasDeployment

    @property
    def infrastructure_model(self) -> str:
        """Return the infrastructure model name."""
        return "openstack-machines"

    def get_client(self) -> Client:
        """Return a client for the deployment."""
        if self.clusterd_address is None:
            raise ValueError("Clusterd address not set.")
        if self._client is None:
            self._client = Client.from_http(self.clusterd_address)
        return self._client

    def get_clusterd_http_address(self) -> str:
        """Return the address of the clusterd server."""
        if self.clusterd_address is None:
            raise ValueError("Clusterd address not set.")
        return self.clusterd_address

    def generate_preseed(self, console) -> str:
        """Generate preseed for deployment."""
        try:
            client = self.get_client()
        except ValueError:
            client = None

        # to avoid circular import
        from sunbeam.provider.maas.client import MaasClient

        maas_client = MaasClient.from_deployment(self)

        preseed_content = ["deployment:"]

        variables = {}
        try:
            if client is not None:
                variables = load_answers(client, PROXY_CONFIG_KEY)
        except ClusterServiceUnavailableException:
            pass

        if not variables:
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

        variables = {}
        try:
            if client is not None:
                variables = load_answers(client, CLOUD_CONFIG_SECTION)
        except ClusterServiceUnavailableException:
            pass
        user_bank = QuestionBank(
            questions=maas_user_questions(maas_client),
            console=console,
            previous_answers=variables.get("user"),
        )
        preseed_content.extend(show_questions(user_bank, section="user"))
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

        preseed_content.extend(PluginManager().get_preseed_questions_content(self))

        preseed_content_final = "\n".join(preseed_content)
        return preseed_content_final

    def get_default_proxy_settings(self) -> dict:
        """Return default proxy settings"""
        # to avoid circular import
        from sunbeam.provider.maas.client import MaasClient

        maas_client = MaasClient.from_deployment(self)
        proxy = maas_client.get_http_proxy()
        if proxy is None:
            return {}

        subnets = maas_client.get_subnets()
        subnets_cidr = (subnet.get("cidr") for subnet in subnets if subnet.get("cidr"))
        no_proxy = ",".join(subnets_cidr)
        return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": no_proxy}


def is_maas_deployment(deployment: Deployment) -> TypeGuard[MaasDeployment]:
    """Check if deployment is a MAAS deployment."""
    return isinstance(deployment, MaasDeployment)


def maas_user_questions(
    maas_client: "sunbeam.provider.maas.client.MaasClient",
) -> dict[str, Question]:
    questions = user_questions()
    questions["nameservers"].default_function = lambda: " ".join(
        maas_client.get_dns_servers()
    )
    # On MAAS, access is always remote
    questions.pop("remote_access_location", None)
    return questions
