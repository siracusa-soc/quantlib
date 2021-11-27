#
# harmonize.py
#
# Author(s):
# Georg Rutishauser <georgr@iis.ee.ethz.ch>
# Moritz Scherer <scheremo@iis.ee.ethz.ch>
#
# Copyright (c) 2020-2021 ETH Zurich.
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

from typing import Optional, Union
import operator

import torch
from torch import nn, fx

from quantlib.algorithms.pact.pact_ops import *

from .pact_util import PACT_symbolic_trace
from .. import FxPass, SequentialPass, InsertModuleBetweenModulesPass, RetracePass


class OpTree:

    def __init__(self, end_node: fx.Node):
        self.end_node = end_node
        self.nodes = [end_node]
        self.open_branches = len(end_node.all_input_nodes)
        assert self.open_branches > 0, "Tried to create OpTree with no branches - something is wrong!"
        # assume that order and assignment of args and kwargs does not matter
        # and they are all treated the same.
        self._args = list(end_node.args) + [v for v in end_node.kwargs.values()]
        # note the users of the final node now - it may get
        # deleted and then end_node.users becomes useless
        self.users = [u for u in end_node.users]

    def add_node(self, node: fx.Node):
        assert node not in self.nodes, "OpTree.add_node(): something went wrong: you tried to add the same node to a tree twice..."
        assert not self.is_terminated, "Tried to add a node to a terminated tree!"
        self.nodes.append(node)
        # we assume that no node in our tree has more than 1 user
        self.open_branches += (len(node.all_input_nodes) - 1)

    def terminate_branch(self):
        # one branch has reached a node which is not part of the tree, so one
        # branch can be subtracted
        assert not self.is_terminated, "Tried to terminate a branch in an already-terminated tree!"
        self.open_branches -= 1

    @property
    def is_terminated(self):
        return self.open_branches == 0

    @property
    def args(self):
        # really ugly list comprehensions:
        # the inputs to the tree is the list of all inputs to all nodes in the
        # tree, except those inputs which are tree nodes themselves.
        # >>><<<
        # spmatteo: this seems the "inbound 1-frontier of the tree";
        #           possible (elegant?) alternative:
        #
        #           def tuplify(x):
        #               return (x,) if not hasattr(x, '__iter__') else tuple(item_ for item_ in x)
        #
        #           all_args = set().union(*[set(tuplify(node.args)) | set(tuplify(node.kwargs.values())) for node in self.nodes])
        #           all_args = all_args.difference(self.nodes)
        #
        # >>><<<
        all_args = [
            arg for node in self.nodes
            for arg in node.args if arg not in self.nodes
        ] + [
            v for node in self.nodes
            for v in node.kwargs.values() if v not in self.nodes
        ]
        # in the case of concat nodes, the arguments are lists or tuples, so we
        # unpack them
        all_args_unpacked = []
        for arg in all_args:
            if isinstance(arg, (list, tuple)):
                all_args_unpacked += [a for a in arg]
            else:
                all_args_unpacked.append(arg)
        return tuple(all_args_unpacked)


class OpTreeReplacementPass(FxPass):

    def __init__(self,
                 node_specs: list,
                 replacement_fn: callable,
                 name: str = '',
                 always_terminate: bool = False):
        super(OpTreeReplacementPass, self).__init__()
        self.node_specs = node_specs
        self.replacement_fn = replacement_fn
        self.name = name
        self.always_terminate = always_terminate

    @staticmethod
    def node_matches_spec(node: fx.Node, node_spec: tuple):
        # >>><<<
        # spmatteo: it seems that `node_spec` is a pair;
        #           what are the types of its two components supposed to be?
        #           By looking at the `AddReplacementPass` and `ConcatReplacementPass`
        #           classes, it seems that:
        #             - the first item is a string corresponding to a `torch.fx`
        #               node class, as can be found in the table "A quick primer on graphs"
        #               in `torch.fx`'s documentation (https://pytorch.org/docs/stable/fx.html)
        #             - the second item is an iterable containing several classes/functions
        #               that are instances of PyTorch's functional API or even of the standard
        #               Python library, such as `operator.add`.
        # >>><<<
        return node.op == node_spec[0] and node.target in node_spec[1]

    @staticmethod
    def trace_op_trees(node: fx.Node,
                       node_specs: list,
                       cur_tree: Union[None, OpTree],
                       op_trees: list,
                       seen_nodes: Optional[set],
                       always_terminate: bool = False):
        # >>><<<
        # spmatteo: the mechanic of this function is not straightforward to me.
        #           I get that it is a recursive function.
        #           I get that it aims at finding "trees" of operations which I suppose
        #           are binary, e.g., additions, concatenations (including the `torch.stack`
        #           concatenation "variant").
        #           It looks to me like it performs depth-first search (DFS) over the
        #           computational graph.
        #           A problematic aspect is the overwriting of symbol `cur_tree`: the logic
        #           of its overwriting is not well-documented.
        #
        #           I labelled the branches below. I will now try to reformulate the flow
        #           in what I think might be a smoother form.
        #
        #           BRANCH A
        #           if node in seen_nodes:  # due to DFS, the traversal of said node has already run to completion
        #               ...
        #           BRANCH B
        #           elif node not in seen_nodes:
        #               if cur_tree is None:
        #                   if node_matches_any_spec(node, specs):
        #                       assert len(node.users) > 0  # the recursive call means that this node is input to some other node
        #                       [start sub-tree traversal]
        #                   else:
        #                       pass  # will proceed to DFS recursive call
        #               else:  # I am already in a tree of binary ops
        #                   if node_matches_any_spec(node, specs):
        #                       assert len(node.users) > 0  # the recursive call means that this node is input to some other node
        #                       if len(node.users) > 1:  # this node is the entry of a branching flow (e.g., ResNets)
        #                           [start traversal of new sub-tree]
        #                           [notify parent sub-tree that this branch has been explored]
        #                           !!! the notification of sub-tree traversal should happen after the DFS recursive call, not before!
        #                           REMARK: the DFS choice in combination with the appending `op_trees` mechanics is indeed clever.
        #                                   In fact, this ensures that in case a binary tree is a "dependency" for a branching point,
        #                                   it will be traversed before its "dependants", and therefore put in the list `op_trees` before
        #                                   its dependants; at epsilon-alignment time, this will ensure that the epsilon consistency
        #                                   is correctly computed over all the input branches.
        #                       elif len(node.users) == 1:
        #                           [add node to sub-tree]
        #                           !!! traversal of sub-tree should happen before adding the node!
        #                   else:
        #                       [terminate traversal of current sub-tree]
        #                       [notify parent sub-tree that this branch has been explored]
        #                       !!! the notification of sub-tree traversal should happen after the DFS recursive call, not before!
        #                       [reset DFS travesal from this point]
        #
        # >>><<<

        # BRANCH A
        if node in seen_nodes:
            # if we have already seen this node, it is either already part of a
            # tree or it will never be, so if we exited a tree, terminate the
            # branch and return
            if cur_tree is not None:
                cur_tree.terminate_branch()
                if cur_tree.is_terminated:
                    op_trees.append(cur_tree)
            return

        # BRANCH B
        seen_nodes.add(node)
        # BRANCH B.1
        if any(
                OpTreeReplacementPass.node_matches_spec(node, spec)
                for spec in node_specs):
            # the current node belongs to a tree
            # BRANCH B.1.i
            if cur_tree is not None and (len(node.users) > 1
                                         or always_terminate):
                # there is a branch, so we need to cut the tree and start a new one
                cur_tree.terminate_branch()
                if cur_tree.is_terminated:
                    op_trees.append(cur_tree)
                cur_tree = OpTree(end_node=node)
            # BRANCH B.1.ii
            elif cur_tree is None:
                cur_tree = OpTree(end_node=node)
            # BRANCH B.1.iii
            else:
                cur_tree.add_node(node)
        # BRANCH B.2
        elif cur_tree is not None:
            # we exited a tree => terminate this branch
            cur_tree.terminate_branch()
            if cur_tree.is_terminated:
                op_trees.append(cur_tree)
            cur_tree = None

        # (still BRANCH B) RECURSIVE DEPTH-FIRST CALL
        # follow the graph upstream
        for inp in node.all_input_nodes:
            OpTreeReplacementPass.trace_op_trees(inp, node_specs, cur_tree,
                                                 op_trees, seen_nodes,
                                                 always_terminate)

        # (implicit) return of BRANCH B

    def run_pass(self, gm: fx.GraphModule):
        out_node = list(gm.graph.nodes)[-1]
        op_trees = []
        self.trace_op_trees(out_node, self.node_specs, None, op_trees, set(),
                            self.always_terminate)
        # we have the op trees, now replace them with a module
        for i, tree in enumerate(op_trees):
            # then add the submodule
            module = self.replacement_fn(tree)
            new_target = f"_QL_OP_TREE_REPLACE_{self.name.upper()}{'_' if self.name != '' else ''}{i}"
            gm.add_submodule(new_target, module)
            # add a node for the submodule call
            with gm.graph.inserting_before(tree.end_node):
                new_node = gm.graph.call_module(new_target, args=tree.args)
            # attach the module to the previous users of the tree's end node
            tree.end_node.replace_all_uses_with(new_node)
            # finally, delete the nodes in the tree
            for node in tree.nodes:
                gm.graph.erase_node(node)

        # and we're done...
        return gm


class AddTreeReplacementPass(OpTreeReplacementPass):
    add_node_specs = [('call_function', (torch.add, operator.add)),
                      ('call_method', ('add',))]

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        super(AddTreeReplacementPass,
              self).__init__(node_specs=self.add_node_specs,
                             replacement_fn=self.add_replacement_fn,
                             name="ADDITION")

    def add_replacement_fn(self, tree):
        return PACTIntegerAdd(num_args=len(tree.args),
                              act_kind='identity',
                              **self.kwargs)


class ConcatTreeReplacementPass(SequentialPass):
    cat_node_specs = [('call_function', (torch.cat,))]
    stack_node_specs = [('call_function', (torch.stack,))]

    def __init__(self,
                 n_levels: int = 256,
                 init_clip: str = 'max',
                 nb_std: float = 3.):
        self.n_levels = n_levels
        self.init_clip = init_clip
        self.nb_std = nb_std
        passes = []
        passes.append(
            OpTreeReplacementPass(node_specs=self.cat_node_specs,
                                  replacement_fn=self.cat_replacement_fn,
                                  name="CONCAT",
                                  always_terminate=True))
        passes.append(
            OpTreeReplacementPass(node_specs=self.stack_node_specs,
                                  replacement_fn=self.stack_replacement_fn,
                                  name="STACK",
                                  always_terminate=True))
        super(ConcatTreeReplacementPass,
              self).__init__(*passes, name_prefix="_QL_REPLACE_CAT_STACK")

    def cat_replacement_fn(self, tree):
        return PACTIntegerConcat(num_args=len(tree.args),
                                 n_levels=self.n_levels,
                                 act_kind='identity',
                                 init_clip=self.init_clip,
                                 nb_std=self.nb_std,
                                 stack_flag=False)

    def stack_replacement_fn(self, tree):
        return PACTIntegerConcat(num_args=len(tree.args),
                                 n_levels=self.n_levels,
                                 act_kind='identity',
                                 init_clip=self.init_clip,
                                 nb_std=self.nb_std,
                                 stack_flag=True)


class InsertActivationsBetweenLinearsPass(InsertModuleBetweenModulesPass):
    before_modules = (nn.Conv1d, nn.Conv2d, nn.Conv3d, nn.BatchNorm1d,
                      nn.BatchNorm2d, nn.BatchNorm3d, nn.Linear)
    after_modules = (nn.Conv1d, nn.Conv2d, nn.Conv3d, nn.Linear)

    def __init__(self, signed: bool = True, **kwargs):
        name = "PACT_LINEAR_ACTIVATIONS"
        self.signed = signed
        self.kwargs = kwargs
        super(InsertActivationsBetweenLinearsPass,
              self).__init__(modules_before=self.before_modules,
                             modules_after=self.after_modules,
                             make_module_fn=self.inserted_module,
                             name=name,
                             combine='force')

    def inserted_module(self, *args, **kwargs):
        if self.signed:
            return PACTAsymmetricAct(**self.kwargs)
        else:
            module_kwargs = {
                k: v for k, v in self.kwargs.items() if k != "symm"
            }
            return PACTUnsignedAct(**module_kwargs)


class HarmonizePACTNetPass(SequentialPass):

    def __init__(self, **kwargs):
        passes = []
        passes.append(RetracePass(PACT_symbolic_trace))
        passes.append(AddTreeReplacementPass(**kwargs))
        actpass_kwargs = {
            k: v for k, v in kwargs.items() if k != 'force_out_eps'
        }
        passes.append(
            InsertActivationsBetweenLinearsPass(signed=True,
                                                act_kind='identity',
                                                **actpass_kwargs))
        super(HarmonizePACTNetPass,
              self).__init__(*passes, name_prefix='_HARMONIZE_PACT_NET_PASS')
