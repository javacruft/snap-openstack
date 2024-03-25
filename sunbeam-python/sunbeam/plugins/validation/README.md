# Validation

This plugin provides OpenStack Integration Test Suite: tempest for Sunbeam. It
is based on [tempest-k8s][1] and [tempest-rock][2] project.

## Installation

To enable cloud validation, you need an already bootstrapped Sunbeam instance.
Then, you can install the plugin with:

```bash
sunbeam enable validation
```

This plugin is also related to the `observability` plugin.

## Contents

This plugin will install [tempest-k8s][1], and provide the `validation`
subcommand to sunbeam client. For more information, please run

```
sunbeam validation --help
```

Additionally, if you enable `observability` plugin, you will also get the
periodic cloud validation feature from this plugin. The loki alert rules for
validation results (e.g. when some tempest tests failed) will be configured, and
a summary of validation results will also be shown in Grafana dashboard.

You can configure the periodic validation schedule using `configure` subcommand
in sunbeam client. For more information, please run

```
sunbeam configure validation --help
```

## Removal

To remove the plugin, run:

```bash
sunbeam disable validation
```

[1]: https://opendev.org/openstack/sunbeam-charms/src/branch/main/charms/tempest-k8s
[2]: https://github.com/canonical/ubuntu-openstack-rocks/tree/main/rocks/tempest
