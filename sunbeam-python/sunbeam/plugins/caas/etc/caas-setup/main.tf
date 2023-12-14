terraform {
  required_version = ">= 0.14.0"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.52.1"
    }
  }
}

provider "openstack" {}

resource "openstack_images_image_v2" "fedora-coreos" {
  name             = "fedora-coreos-38"
  image_source_url = "https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/38.20230806.3.0/x86_64/fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2.xz"
  container_format = "bare"
  disk_format      = "qcow2"
  decompress       = true
  visibility       = "public"
  properties = {
    os_distro       = "fedora-coreos"
    architecture    = "x86_64"
    hypervisor_type = "qemu"
  }
}
