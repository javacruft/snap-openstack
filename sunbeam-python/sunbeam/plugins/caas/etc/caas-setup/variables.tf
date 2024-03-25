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

variable "image-name" {
  description = "Image name to set in glance"
  type        = string
  default     = "fedora-coreos-38"
}

variable "image-source-url" {
  description = "Image URL to upload to glance"
  type        = string
  default     = "https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/38.20230806.3.0/x86_64/fedora-coreos-38.20230806.3.0-openstack.x86_64.qcow2.xz"
}

variable "image-container-format" {
  description = "Image container format"
  type        = string
  default     = "bare"
}

variable "image-disk-format" {
  description = "Image disk format"
  type        = string
  default     = "qcow2"
}

variable "image-properties" {
  description = "Properties to set on image in glance"
  type        = map(string)
  default = {
    os_distro       = "fedora-coreos"
    architecture    = "x86_64"
    hypervisor_type = "qemu"
  }
}
