# Container as a Service

This plugin provides Container as a Service project for Sunbeam. It's based on [Magnum](https://docs.openstack.org/magnum/latest/), a Container as a Service project for OpenStack.

## Installation

To enable the Container as a Service project, you need an already bootstraped Sunbeam instance and the following plugins: Secrets, Orchestration, Loadbalancer (optional). Then, you can install the plugin with:

```bash
sunbeam enable caas
```

## Configure

The Container as a Service project needs a compatible image to instanciate kubernetes cluster. The correct image can be setup automatically with:

```bash
sunbeam configure caas
```

The list of compatible images can be found [here](https://docs.openstack.org/magnum/latest/user/index.html#supported-versions).

## Contents

This plugin will install the following services:
- Magnum: Container as a Service project for OpenStack [charm](https://opendev.org/openstack/charm-magnum-k8s) [ROCK](https://github.com/canonical/ubuntu-openstack-rocks/tree/main/rocks/magnum-consolidated)
- MySQL Router for Magnum [charm](https://github.com/canonical/mysql-router-k8s-operator) [ROCK](https://github.com/canonical/charmed-mysql-rock)
- MySQL Instance in the case of a multi-mysql installation (for large deployments) [charm](https://github.com/canonical/mysql-k8s-operator) [ROCK](https://github.com/canonical/charmed-mysql-rock)

Services are constituted of charms, i.e. operator code, and ROCKs, the corresponding OCI images.

## Removal

To remove the plugin, run:

```bash
sunbeam disable caas
```
