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

import glob
import ipaddress
import logging
import re
import socket
import sys
from pathlib import Path
from typing import Dict, List

import click
import netifaces
import pwgen
from pyroute2 import IPDB, NDB

LOG = logging.getLogger(__name__)
LOCAL_ACCESS = "local"
REMOTE_ACCESS = "remote"


def is_nic_connected(iface_name: str) -> bool:
    """Check if nic is physically connected."""
    with IPDB() as ipdb:
        state = ipdb.interfaces[iface_name].operstate
        # pyroute2 does not seem to expose the states as
        # consumable constants
        return state == "UP"


def is_nic_up(iface_name: str) -> bool:
    """Check if nic is up."""
    with NDB() as ndb:
        state = ndb.interfaces[iface_name]["state"]
        return state.upper() == "UP"


def get_hypervisor_hostname() -> str:
    """Get FQDN as per libvirt."""
    # Use same logic used by libvirt
    # https://github.com/libvirt/libvirt/blob/a5bf2c4bf962cfb32f9137be5f0ba61cdd14b0e7/src/util/virutil.c#L406
    hostname = socket.gethostname()
    if "." in hostname:
        return hostname

    addrinfo = socket.getaddrinfo(
        hostname, None, family=socket.AF_UNSPEC, flags=socket.AI_CANONNAME
    )
    for addr in addrinfo:
        fqdn = addr[3]
        if fqdn and fqdn != "localhost":
            return fqdn

    return hostname


def get_fqdn() -> str:
    """Get FQDN of the machine"""
    # If the fqdn returned by this function and from libvirt are different,
    # the hypervisor name and the one registered in OVN will be different
    # which leads to port binding errors,
    # see https://bugs.launchpad.net/snap-openstack/+bug/2023931

    fqdn = get_hypervisor_hostname()
    if "." in fqdn:
        return fqdn

    # Deviation from libvirt logic
    # Try to get fqdn from IP address as a last resort
    ip = get_local_ip_by_default_route()
    try:
        fqdn = socket.getfqdn(socket.gethostbyaddr(ip)[0])
        if fqdn != "localhost":
            return fqdn
    except Exception as e:
        LOG.debug("Ignoring error in getting FQDN")
        LOG.debug(e, exc_info=True)

    # return hostname if fqdn is localhost
    return socket.gethostname()


def get_ifaddresses_by_default_route() -> dict:
    """Get address configuration from interface associated with default gateway."""
    interface = "lo"
    ip = "127.0.0.1"
    netmask = "255.0.0.0"

    # TOCHK: Gathering only IPv4
    if "default" in netifaces.gateways():
        interface = netifaces.gateways()["default"][netifaces.AF_INET][1]

    ip_list = netifaces.ifaddresses(interface)[netifaces.AF_INET]
    if len(ip_list) > 0 and "addr" in ip_list[0]:
        return ip_list[0]

    return {"addr": ip, "netmask": netmask}


def get_local_ip_by_default_route() -> str:
    """Get IP address of host associated with default gateway."""
    return get_ifaddresses_by_default_route()["addr"]


def get_local_cidr_by_default_routes() -> str:
    """Get CIDR of host associated with default gateway"""
    conf = get_ifaddresses_by_default_route()
    ip = conf["addr"]
    netmask = conf["netmask"]
    network = ipaddress.ip_network(f"{ip}/{netmask}", strict=False)
    return str(network)


def get_nic_macs(nic: str) -> list:
    """Return list of mac addresses associates with nic."""
    addrs = netifaces.ifaddresses(nic)
    return sorted([a["addr"] for a in addrs[netifaces.AF_LINK]])


def filter_link_local(addresses: List[Dict]) -> List[Dict]:
    """Filter any IPv6 link local addresses from configured IPv6 addresses."""
    if addresses is None:
        return None
    return [addr for addr in addresses if "fe80" not in addr.get("addr")]


def is_configured(nic: str) -> bool:
    """Whether interface is configured with IPv4 or IPv6 address."""
    addrs = netifaces.ifaddresses(nic)
    return bool(
        addrs.get(netifaces.AF_INET) or filter_link_local(addrs.get(netifaces.AF_INET6))
    )


def get_free_nics(include_configured=False) -> list:
    """Return a list of nics which doe not have a v4 or v6 address."""
    virtual_nic_dir = "/sys/devices/virtual/net/*"
    virtual_nics = [Path(p).name for p in glob.glob(virtual_nic_dir)]
    bond_nic_dir = "/sys/devices/virtual/net/*/bonding"
    bonds = [Path(p).parent.name for p in glob.glob(bond_nic_dir)]
    bond_macs = []
    for bond_iface in bonds:
        bond_macs.extend(get_nic_macs(bond_iface))
    candidate_nics = []
    for nic in netifaces.interfaces():
        if nic in bonds and not is_configured(nic):
            LOG.debug(f"Found bond {nic}")
            candidate_nics.append(nic)
            continue
        macs = get_nic_macs(nic)
        if list(set(macs) & set(bond_macs)):
            LOG.debug(f"Skipping {nic} it is part of a bond")
            continue
        if nic in virtual_nics:
            LOG.debug(f"Skipping {nic} it is virtual")
            continue
        if is_configured(nic) and not include_configured:
            LOG.debug(f"Skipping {nic} it is configured")
        else:
            LOG.debug(f"Found nic {nic}")
            candidate_nics.append(nic)
    return candidate_nics


def get_free_nic() -> str:
    """Return a single candidate nic."""
    nics = get_free_nics()
    nic = ""
    if len(nics) > 0:
        nic = nics[0]
    return nic


def get_nameservers(ipv4_only=True) -> List[str]:
    """Return a list of nameservers used by the host."""
    resolve_config = Path("/run/systemd/resolve/resolv.conf")
    nameservers = []
    try:
        with open(resolve_config, "r") as f:
            contents = f.readlines()
        nameservers = [
            line.split()[1] for line in contents if re.match(r"^\s*nameserver\s", line)
        ]
        if ipv4_only:
            nameservers = [n for n in nameservers if not re.search("[a-zA-Z]", n)]
    except FileNotFoundError:
        nameservers = []
    return nameservers


def generate_password() -> str:
    """Generate a password."""
    return pwgen.pwgen(12)


class CatchGroup(click.Group):
    """Catch exceptions and print them to stderr."""

    def __call__(self, *args, **kwargs):
        try:
            return self.main(*args, **kwargs)
        except Exception as e:
            LOG.debug(e, exc_info=True)
            message = (
                "An unexpected error has occurred."
                " Please run 'sunbeam inspect' to generate an inspection report."
            )
            LOG.warn(message)
            LOG.error("Error: %s", e)
            sys.exit(1)
