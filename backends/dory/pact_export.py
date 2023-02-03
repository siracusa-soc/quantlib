#
# pact_export.py
#
# Author(s):
# Georg Rutishauser <georgr@iis.ee.ethz.ch>
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

from functools import partial
from pathlib import Path

from torch.onnx import OperatorExportTypes, CheckerError
import torch
from torch import nn
import torchvision

import onnx
import numpy as np

import quantlib.editing.fx as qlfx
from quantlib.editing.lightweight import LightweightGraph
from quantlib.algorithms.pact import RequantShift, PACTIntegerLayerNorm, PACTIntegerGELU, PACTWrapMHSA, PACTWrapModule
from .dory_passes import AvgPoolWrap, DORYAdder, DORYHarmonizePass

# annotate:
#   conv, FC nodes:
#     - 'weight_bits'
#     - 'bias_bits'
#   clip nodes (these represent [re]quantization nodes):
#     - 'out_bits'
#   Mul nodes:
#     - 'mult_bits' - this describes the multiplicative factor as well as the
#                    result's precision
#   Add nodes:
#     - 'add_bits' - this describes the added number's as well as the result's precision



def get_attr_by_name(node, name):
    try:
        a = [attr for attr in node.attribute if attr.name == name][0]
    except IndexError:
        a = "asdf"
    return a


def annotate_onnx(m, prec_dict : dict, requant_bits : int = 32):

# annotate all clip nodes - use the clip limits to determine the number of
# bits; in a properly exported model this would be done based on
# meta-information contained in the pytorch graph.
    clip_nodes = [n for n in m.graph.node if n.op_type == "Clip"]
    for i, n in enumerate(clip_nodes):
        lower = get_attr_by_name(n, "min").f
        #assert lower == 0.0, "clip node {} has lower clip bound {} not equal to zero!".format(n.name, lower)
        upper = get_attr_by_name(n, "max").f
        n_bits = int(np.round(np.log2(upper-lower+1.0)))
        n_bits = n_bits if n_bits <= 8 else 32
        precision_attr = onnx.helper.make_attribute(key='out_bits', value=n_bits)
        n.attribute.append(precision_attr)


    conv_fc_nodes = [n for n in m.graph.node if n.op_type in ['Conv', 'Gemm']]
    for n in conv_fc_nodes:
        weight_name = n.input[1].rstrip('.weight')
        weight_bits = prec_dict[weight_name]
        weight_attr = onnx.helper.make_attribute(key='weight_bits', value=weight_bits)
        n.attribute.append(weight_attr)
        # bias accuracy hardcoded to 32b.
        bias_attr = onnx.helper.make_attribute(key='bias_bits', value=32)
        n.attribute.append(bias_attr)

    # assume that add nodes have 32b precision?? not specified in the name...
    add_nodes_requant = [n for n in m.graph.node if n.op_type == 'Add' and not all(i.isnumeric() for i in n.input)]
    # the requantization add nodes are the ones not adding two operation nodes'
    # outputs together
    for n in add_nodes_requant:
        add_attr = onnx.helper.make_attribute(key='add_bits', value=requant_bits)
        n.attribute.append(add_attr)

    add_nodes_residual = [n for n in m.graph.node if n.op_type == 'Add' and n not in add_nodes_requant]
    for n in add_nodes_residual:
        # assume that all residual additions are executed in 8b
        add_attr = onnx.helper.make_attribute(key='add_bits', value=8)
        n.attribute.append(add_attr)

    mult_nodes = [n for n in m.graph.node if n.op_type == 'Mul']
    for n in mult_nodes:
        mult_attr = onnx.helper.make_attribute(key='mult_bits', value=requant_bits)
        n.attribute.append(mult_attr)

def export_net(net : nn.Module,name : str, out_dir : str, eps_in : float, in_data : torch.Tensor, integerize : bool = True, D : float = 2**24, opset_version : int  = 10, align_avg_pool : bool = False):
    net = net.eval()


    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    onnx_file = f"{name}_ql_integerized.onnx"
    onnx_path = out_path.joinpath(onnx_file)

    shape_in = in_data.shape

    if integerize:
        net_traced = qlfx.passes.pact.PACT_symbolic_trace(net)

        int_pass = qlfx.passes.pact.IntegerizePACTNetPass(shape_in=shape_in,  eps_in=eps_in, D=D)
        net_integerized = int_pass(net_traced)
    else: # assume the net is already integerized
        net_integerized = net

    if align_avg_pool:
        align_avgpool_pass = DORYHarmonizePass(in_shape=shape_in)
        net_integerized = align_avgpool_pass(net_integerized)

    integerized_nodes = LightweightGraph.build_nodes_list(net_integerized, leaf_types=(AvgPoolWrap, DORYAdder, PACTWrapMHSA))

    # the integerization pass annotates the conv layers with the number of
    # weight levels. from this information we can make a dictionary of the number of
    # weight bits.
    prec_dict = {}

    for name, module in net_integerized.named_modules():
        if isinstance(module, (nn.Conv1d, nn.Conv2d, nn.Linear)):
            n_bits = int(np.log2(module.n_levels+1.2))
            prec_dict[name] = n_bits

    #first export an unannotated ONNX graph
    test_input = torch.rand(shape_in)
    try:
        torch.onnx.export(net_integerized.to('cpu'),
                      test_input,
                      str(onnx_path),
                      export_params=True,
                      verbose=False,
                      opset_version=opset_version,
                      do_constant_folding=True,
                      keep_initializers_as_inputs=True)
    except CheckerError as e:
        pass


    #load the exported model and annotate it
    onnx_model = onnx.load(str(onnx_path))
    annotate_onnx(onnx_model, prec_dict)
    # finally, save the annotated ONNX model
    onnx.save(onnx_model, str(onnx_path))
    # now we pass a test input through the model and log the intermediate
    # activations

    # make a forward hook to dump outputs of RequantShift layers
    acts = []
    def dump_hook(self, inp, outp, name):
        # DORY wants HWC tensors
        acts.append((name, torch.floor(outp[0])))

    for n in integerized_nodes:
        if isinstance(n.module, (RequantShift, nn.AdaptiveAvgPool1d, nn.AdaptiveAvgPool2d, nn.AdaptiveAvgPool3d, nn.AvgPool1d, nn.AvgPool2d, nn.AvgPool3d, nn.MaxPool1d, nn.MaxPool2d, nn.MaxPool3d, nn.Linear, AvgPoolWrap, DORYAdder, PACTIntegerLayerNorm, PACTIntegerGELU, PACTWrapMHSA)):
            hook = partial(dump_hook, name=n.name)
            n.module.register_forward_hook(hook)

    # open the supplied input image
    if in_data is not None:
        im_tensor = in_data.clone().to(dtype=torch.float64)
        net_integerized = net_integerized.to(dtype=torch.float64)
        output = net_integerized(im_tensor).to(dtype=torch.float64)
        # now, save everything into beautiful text files
        def save_beautiful_text(t : torch.Tensor, layer_name : str, filename : str):
            t = t.squeeze(0)

            if t.dim()==3:
                # expect a (C, H, W) tensor - DORY expects (H, W, C)
                t = t.permute(1,2,0)
            #SCHEREMO: HACK HACK HACK HACK HACK HACK
            elif t.dim()==2:
#                 # expect a (C, D) tensor - DORY expects (D, C)
                t = t.permute(1,0)
            else:
                print(f"Not permuting output of layer {layer_name}...")

            filepath = out_path.joinpath(f"{filename}.txt")
            with open(str(filepath), 'w') as fp:
                fp.write(f"# {layer_name} (shape {list(t.shape)}),\n")
                for el in t.flatten():
                    fp.write(f"{int(el)},\n")
        save_beautiful_text(im_tensor, "input", "input")
        save_beautiful_text(output, "output", "output")
        for i, (name, t) in enumerate(acts):
            save_beautiful_text(t, name, f"out_layer{i}")

    #done!
