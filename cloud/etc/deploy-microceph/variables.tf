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

variable "charm_microceph_channel" {
  description = "Operator channel for microceph deployment"
  type        = string
  default     = "reef/candidate"
}

variable "charm_microceph_revision" {
  description = "Operator channel revision for microceph deployment"
  type        = number
  default     = null
}

variable "charm_microceph_config" {
  description = "Operator config for microceph deployment"
  type        = map(string)
  default     = {}
}

variable "microceph_channel" {
  description = "K8S channel to deploy, not the operator channel"
  default     = "reef/stable"
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
