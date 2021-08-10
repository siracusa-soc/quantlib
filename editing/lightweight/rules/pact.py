# pact.py
#
# Author(s):
# Georg Rutishauser <georgr@iis.ee.ethz.ch>
#
# Copyright (c) 2020-2021 ETH Zurich. All rights reserved.
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

from typing import Union
from functools import partial

from torch import nn

from quantlib.algorithms.pact.pact_ops import *
from .rules import LightweightRule
from .filters import Filter

def replace_pact_conv_linear(module : Union[nn.Conv1d, nn.Conv2d, nn.Linear],
                             **kwargs):
    if isinstance(module, nn.Conv1d):
        return PACTConv1d.from_conv1d(module, **kwargs)
    elif isinstance(module, nn.Conv2d):
        return PACTConv2d.from_conv2d(module, **kwargs)
    elif isinstance(module, nn.Linear):
        return PACTLinear.from_linear(module, **kwargs)
    else:
        raise TypeError(f"Incompatible module of type {module.__class__.__name__} passed to replace_pact_conv_linear!")

def replace_pact_unsigned_act(module : nn.Module,
                     **kwargs):
    if 'act_kind' not in kwargs.keys():
        if isinstance(module, nn.ReLU6):
            act_kind = 'relu6'
        elif isinstance(module, nn.LeakyReLU):
            act_kind = 'leaky_relu'
            if 'leaky' not in kwargs:
                kwargs['leaky'] = module.negative_slope
        else: # default activation is ReLU
            act_kind = 'relu'

        kwargs['act_kind'] = act_kind

    return PACTUnsignedAct(**kwargs)

class ReplaceConvLinearPACTRule(LightweightRule):
    def __init__(self,
                 filter_: Filter,
                 **kwargs):
        replacement_fun = partial(replace_pact_conv_linear, **kwargs)
        super(ReplaceConvLinearPACTRule, self).__init__(filter_=filter_, replacement_fun=replacement_fun)

class ReplaceActPACTRule(LightweightRule):
    def __init__(self,
                 filter_: Filter,
                 **kwargs):
        replacement_fun = partial(replace_pact_unsigned_act, **kwargs)
        super(ReplaceActPACTRule, self).__init__(filter_=filter_, replacement_fun=replacement_fun)
