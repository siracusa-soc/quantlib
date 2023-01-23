# 
# Author(s):
# Francesco Conti <f.conti@unibo.it>
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

import itertools
import torch.fx as fx
import torch

from ..remover.applicationpoint import EpsTunnelNode
from quantlib.editing.editing.editors import Applier


class FinalEpsTunnelRemoverApplier(Applier):

    def _apply(self, g: fx.GraphModule, ap: EpsTunnelNode, id_: str) -> fx.GraphModule:

        node = ap.node

        # the `fx.Node` is functionally equivalent to the identity, so we connect its (unique) input to all the outputs
        predecessors = {p for p in node.all_input_nodes}  # upstream
        assert len(predecessors) == 1
        successors = {s for s in node.users}  # downstream
        assert len(successors) == 1
        for p, s in itertools.product(predecessors, successors):
            s.replace_input_with(node, p)
        torch.set_printoptions(precision=16)
        print("[FinalEpsTunnelRemover] %s: removing EpsTunnel with scaling factor %s" % (s, g.get_submodule(node.target).eps_out/g.get_submodule(node.target).eps_in))
        print("[FinalEpsTunnelRemover] %s: outputs will need to be scaled *externally* to maintain program semantics." % (s,))
        torch.set_printoptions()

        g.delete_submodule(node.target)
        g.graph.erase_node(node)

        return g