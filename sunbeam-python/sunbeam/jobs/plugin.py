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

import importlib
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

import click
import yaml
from snaphelpers import Snap

from sunbeam import utils
from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import (
    ClusterServiceUnavailableException,
    ConfigItemNotFoundException,
)
from sunbeam.jobs.common import read_config
from sunbeam.jobs.deployment import Deployment
from sunbeam.jobs.manifest import SoftwareConfig

LOG = logging.getLogger(__name__)
PLUGIN_YAML = "plugins.yaml"
# Plugin-<repo plugin name>
EXTERNAL_REPO_PLUGIN_KEY = "Plugin-repo"


class PluginManager:
    """Class to expose functions to interact with plugins.

    Implement the functions required either by sunbeam
    cli or any other cluster operations that need to be
    triggered on all or some of the plugins.
    """

    @classmethod
    def get_external_plugins_base_path(cls) -> Path:
        """Returns the path in snap where external repos are cloned."""
        return Snap().paths.user_data / "plugins"

    @classmethod
    def get_core_plugins_path(cls) -> Path:
        """Returns the path where the core plugins are defined."""
        return Path(__file__).parent.parent / "plugins"

    @classmethod
    def get_plugins_map(
        cls,
        plugin_file: Path,
        raise_exception: bool = False,
    ) -> Dict[str, type]:
        """Return dict of {plugin name: plugin class} from plugin yaml.

        :param plugin_file: Plugin yaml file
        :param raise_exception: If set to true, raises an exception in case
                                plugin class is not loaded. By default, ignores
                                by logging the error message.

        :returns: Dict of plugin classes
        :raises: ModuleNotFoundError or AttributeError
        """
        plugins_yaml = {}
        with plugin_file.open() as file:
            plugins_yaml = yaml.safe_load(file)

        plugins = plugins_yaml.get("sunbeam-plugins", {}).get("plugins", [])
        plugin_classes = {}

        for plugin in plugins:
            module = None
            plugin_class = plugin.get("path")
            if plugin_class is None:
                continue
            module_class_ = plugin_class.rsplit(".", 1)
            try:
                module = importlib.import_module(module_class_[0])
                plugin_class = getattr(module, module_class_[1])
                plugin_classes[plugin["name"]] = plugin_class
            # Catching Exception instead of specific errors as plugins
            # can raise any exception based on implementation.
            except Exception as e:
                # Exceptions observed so far
                # ModuleNotFoundError, AttributeError, NameError
                LOG.debug(str(e))
                LOG.warning(f"Ignored loading plugin: {plugin_class}")
                if raise_exception:
                    raise e

                continue

        LOG.debug(f"Plugin classes: {plugin_classes}")
        return plugin_classes

    @classmethod
    def get_plugin_classes(
        cls, plugin_file: Path, raise_exception: bool = False
    ) -> List[type]:
        """Return a list of plugin classes from plugin yaml.

        :param plugin_file: Plugin yaml file
        :param raise_exception: If set to true, raises an exception in case
                                plugin class is not loaded. By default, ignores
                                by logging the error message.

        :returns: List of plugin classes
        :raises: ModuleNotFoundError or AttributeError
        """
        return list(cls.get_plugins_map(plugin_file, raise_exception).values())

    @classmethod
    def get_all_plugin_classes(cls) -> List[type]:
        """Return a lsit of plugin classes from all repositories."""
        core_plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
        plugins = cls.get_plugin_classes(core_plugin_file)
        for path in cls.get_external_plugins_base_path().glob("*"):
            if not path.is_dir():
                continue
            plugins.extend(cls.get_plugin_classes(path / PLUGIN_YAML))
        return plugins

    @classmethod
    def get_all_external_repos(cls, client: Client, detail: bool = False) -> list:
        """Return all external repos stored in DB.

        Returns just names by default, the format will be
        [<repo1>, <repo2>,...]
        If details is True, returns names and git repo, branch details.
        The format will be
        [
            {"name": <repo1>, "git_repo": <repo url>, "git_branch": <repo branch>},
            {"name": <repo2>, "git_repo": <repo url>, "git_branch": <repo branch>},
            ...
        ]

        :param client: Clusterd client object.
        :param detail: If true, includes repo path and branch as well.
        :returns: List of repos.
        """
        try:
            config = read_config(client, EXTERNAL_REPO_PLUGIN_KEY)
            if detail:
                return config.get("repos", [])
            else:
                repos = [
                    repo.get("name")
                    for repo in config.get("repos", [])
                    if "name" in repo
                ]
                return repos
        except (ConfigItemNotFoundException, ClusterServiceUnavailableException) as e:
            LOG.debug(str(e))
            return []

    @classmethod
    def get_plugins(cls, deployment: Deployment, repos: Optional[list] = []) -> dict:
        """Returns list of plugin name and description.

        Get all plugins information for each repo specified in repos.
        If repos is None or empty list, get plugins for all the repos
        including the internal plugins in snap-openstack repo. Repo name
        core is reserved for internal plugins in snap-openstack repo.

        :param deployment: Deployment instance.
        :param repos: List of repos
        :returns: Dictionary of repo with plugin name and description

        Sample output:
        {
            "core": {
                [
                    ("pro", "Ubuntu pro management plugin"),
                    ("repo", "External plugin repo management"
                ]
            }
        }
        """
        if not repos:
            repos.append("core")
            repos.extend(cls.get_all_external_repos(deployment.get_client()))

        plugins = {}
        for repo in repos:
            if repo == "core":
                plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
            else:
                plugin_file = cls.get_external_plugins_base_path() / repo / PLUGIN_YAML

            plugins_yaml = {}
            with plugin_file.open() as file:
                plugins_yaml = yaml.safe_load(file)

            plugins_list = plugins_yaml.get("sunbeam-plugins", {}).get("plugins", {})
            plugins[repo] = []
            plugins[repo].extend(
                [
                    (plugin.get("name"), plugin.get("description"))
                    for plugin in plugins_list
                ]
            )

        return plugins

    @classmethod
    def enabled_plugins(
        cls, deployment: Deployment, repos: Optional[list] = []
    ) -> list:
        """Returns plugin names that are enabled.

        Get all plugins from the list of repos and return plugins that have enabled
        as True.
        Repo name core is reserved for internal plugins in snap-openstack repo.
        If repos is None or empty list, get plugins from all repos defined in
        cluster db including the internal plugins.

        :param deployment: Deployment instance.
        :param repos: List of repos
        :returns: List of enabled plugins
        """
        enabled_plugins = []
        if not repos:
            repos.append("core")
            repos.extend(cls.get_all_external_repos(deployment.get_client()))

        for repo in repos:
            if repo == "core":
                plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
            else:
                plugin_file = cls.get_external_plugins_base_path() / repo / PLUGIN_YAML
                plugin_repo_path = str(plugin_file.parent)
                if plugin_repo_path not in sys.path:
                    sys.path.append(plugin_repo_path)

            # If the repo folder is already deleted
            if not plugin_file.exists():
                LOG.debug(
                    f"Discarding loading plugins for repo {repo} as Plugin "
                    "yaml does not exist"
                )
                continue

            for plugin in cls.get_plugin_classes(plugin_file):
                p = plugin(deployment)
                if hasattr(plugin, "enabled") and p.enabled:
                    enabled_plugins.append(p.name)

        LOG.debug(f"Enabledplugins in repos {repos}: {enabled_plugins}")
        return enabled_plugins

    @classmethod
    def get_all_plugin_manifests(
        cls, deployment: Deployment
    ) -> dict[str, SoftwareConfig]:
        manifests = {}
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            manifests[plugin.name] = plugin.manifest_defaults()

        return manifests

    @classmethod
    def get_all_plugin_manifest_tfvar_map(cls, deployment: Deployment) -> dict:
        tfvar_map = {}
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            m_dict = plugin.manifest_attributes_tfvar_map()
            utils.merge_dict(tfvar_map, m_dict)

        return tfvar_map

    @classmethod
    def add_manifest_section(
        cls, deployment: Deployment, software_config: SoftwareConfig
    ) -> None:
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            plugin.add_manifest_section(software_config)

    @classmethod
    def get_preseed_questions_content(cls, deployment: Deployment) -> list:
        content = []
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            content.extend(plugin.preseed_questions_content())

        return content

    @classmethod
    def get_all_charms_in_openstack_plan(cls, deployment: Deployment) -> list:
        charms = []
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            m_dict = plugin.manifest_attributes_tfvar_map()
            charms_from_plugin = list(
                m_dict.get("openstack-plan", {}).get("charms", {}).keys()
            )
            charms.extend(charms_from_plugin)

        return charms

    @classmethod
    def update_proxy_model_configs(cls, deployment: Deployment) -> None:
        plugins = cls.get_all_plugin_classes()
        for klass in plugins:
            plugin = klass(deployment)
            plugin.update_proxy_model_configs()

    @classmethod
    def register(
        cls,
        deployment: Deployment,
        cli: click.Group,
    ) -> None:
        """Register the plugins.

        Register both the core plugins in snap-openstack repo and the plugins
        in the external repos added to sunbeam by the user. Once registeted,
        all the commands/groups defined by plugins will be shown as part of
        sunbeam cli.

        :param deployment: Deployment instance.
        :param cli: Main click group for sunbeam cli.
        """
        LOG.debug("Registering core plugins")
        core_plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
        for plugin in cls.get_plugin_classes(core_plugin_file):
            try:
                plugin(deployment).register(cli)
            except ValueError as e:
                LOG.debug("Failed to register plugin: %r", str(plugin))
                if "Clusterd address" in str(e):
                    LOG.debug(
                        "Clusterd address not found. Ignoring plugin registration."
                    )
                    continue
                raise e
        try:
            client = deployment.get_client()
        except ValueError as e:
            if "Clusterd address" in str(e):
                LOG.debug(
                    "Clusterd address unset. Ignoring external plugin registration."
                )
                return
            raise e
        repos = cls.get_all_external_repos(client)
        LOG.debug(f"Registering external repo plugins {repos}")
        for repo in repos:
            plugin_file = cls.get_external_plugins_base_path() / repo / PLUGIN_YAML
            plugin_repo_path = str(plugin_file.parent)
            if plugin_repo_path not in sys.path:
                sys.path.append(plugin_repo_path)

            # If the repo folder is already deleted
            if not plugin_file.exists():
                continue

            for plugin in cls.get_plugin_classes(plugin_file):
                plugin(deployment).register(cli)

    @classmethod
    def resolve_plugin(cls, repo: str, plugin: str) -> Optional[type]:
        """Resolve a plugin name to a class.

        Lookup core and external plugins to find a plugin with the given name.
        """
        if repo == "core":
            plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
        else:
            plugin_file = cls.get_external_plugins_base_path() / repo / PLUGIN_YAML
        plugins = cls.get_plugins_map(plugin_file)

        return plugins.get(plugin)

    @classmethod
    def is_plugin_version_changed(cls, plugin) -> bool:
        """Check if plugin version is changed.

        Compare the plugin version in the database and the newly loaded one
        from plugins.yaml. Return true if versions are different.

        :param plugin: Plugin object
        :returns: True if versions are different.
        """
        LOG.debug("In plugin version changed check")
        if not hasattr(plugin, "get_plugin_info") or not hasattr(plugin, "version"):
            raise AttributeError("Plugin is not a valid plugin class")
        return not plugin.get_plugin_info().get("version", "0.0.0") == str(
            plugin.version
        )

    @classmethod
    def update_plugins(
        cls,
        deployment: Deployment,
        repos: Optional[list] = [],
        upgrade_release: bool = False,
    ) -> None:
        """Call plugin upgrade hooks.

        Get all the plugins defined in repos and call the corresponding plugin
        upgrade hooks if the plugin is enabled and version is changed. Do not
        run any upgrade hooks if repos is empty list.

        :param deployment: Deployment instance.
        :param repos: List of repos
        """
        if not repos:
            return

        for repo in repos:
            LOG.debug(f"Upgrading plugins for repo {repo}")
            if repo == "core":
                plugin_file = cls.get_core_plugins_path() / PLUGIN_YAML
            else:
                plugin_file = cls.get_external_plugins_base_path() / repo / PLUGIN_YAML
                plugin_repo_path = str(plugin_file.parent)
                if plugin_repo_path not in sys.path:
                    sys.path.append(plugin_repo_path)

            # If the repo folder is already deleted
            if not plugin_file.exists():
                continue

            for plugin in cls.get_plugin_classes(plugin_file):
                p = plugin(deployment)
                LOG.debug(f"Object created {p.name}")
                if hasattr(plugin, "enabled"):
                    LOG.debug(f"enabled - {p.enabled}")
                if (
                    hasattr(plugin, "enabled")
                    and p.enabled  # noqa W503
                    and hasattr(plugin, "upgrade_hook")  # noqa W503
                ):
                    LOG.debug(f"Upgrading plugin {p.name} defined in repo {repo}")
                    try:
                        p.upgrade_hook(upgrade_release=upgrade_release)
                    except TypeError:
                        LOG.debug(
                            (
                                f"Plugin {p.name} does not support upgrades "
                                "between channels"
                            )
                        )
                        p.upgrade_hook()
