# 
# Author(s):
# Matteo Spallanzani <spmatteo@iis.ee.ethz.ch>
# 
# Copyright (c) 2020-2022 ETH Zurich and University of Bologna.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# 

"""The object passed between ``ActivationFinder``s and ``ActivationReplacer``s."""

import torch.fx as fx
from typing import NamedTuple

from quantlib.editing.editing.editors.base import ApplicationPoint


# Since `ApplicationPoint` is an `ABC` object and `ABC`'s meta-class is
# `ABCMeta`, and `NamedTuple` is itself a meta-class but different from
# `ABCMeta`, we first need to instantiate a class from `NamedTuple`...

class _ActivationNode(NamedTuple):
    node: fx.Node


# ... and then create our `ApplicationPoint` type.
class ActivationNode(ApplicationPoint, _ActivationNode):
    pass