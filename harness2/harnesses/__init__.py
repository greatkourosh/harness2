# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The harness control plane: registered agent frameworks, each kept separate.

A *harness* is an independent agent framework installed on this machine (pi,
opencode, deepseek, claude code, claude desktop). Each owns its own config file
and is driven through its own CLI; they are peers, never nested. The one thing
they share is the model gateway (9router), which is where their traffic meets.

This package does not run models itself. It reads each harness's own config
format, writes a per-harness model allow-list back into it, and reports where
that harness's own logs live.
"""

from .adapters import ADAPTERS, get_adapter
from .registry import HarnessSpec, load_all, load_one

__all__ = ["HarnessSpec", "load_all", "load_one", "ADAPTERS", "get_adapter"]
