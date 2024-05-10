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

variable "charm_microk8s_channel" {
  description = "Operator channel for microk8s deployment"
  type        = string
  default     = "legacy/stable"
}

variable "charm_microk8s_revision" {
  description = "Operator channel revision for microk8s deployment"
  type        = number
  default     = null
}

variable "charm_microk8s_config" {
  description = "Operator config for microk8s deployment"
  type        = map(string)
  default     = {}
}

variable "microk8s_channel" {
  description = "K8S channel to deploy, not the operator channel"
  default     = "1.28-strict/stable"
}

variable "machine_ids" {
  description = "List of machine ids to include"
  type        = list(string)
  default     = []
}

variable "machine_model" {
  description = "Model to deploy to"
  type        = string
}

variable "addons" {
  description = "Addon configuration to enable on the deployment"
  type        = map(string)
  default = {
    dns              = ""
    hostpath-storage = ""
    metallb          = "10.20.21.1-10.20.21.10"
  }
}
