import torch.nn as nn
import torch.fx as fx
from torch.fx import Tracer
from torch.fx import symbolic_trace
from functools import partial
from typing import Tuple, Dict, Any, Union, Callable, Type

from quantlib.newalgorithms.qmodules.qmodules.qmodules import _QModule
from quantlib.newutils import quantlib_err_header


__all__ = [
    'Tracer',
    'QModuleTracer',
    'symbolic_trace',
    'qmodule_symbolic_trace',
]


class CustomTracer(fx.Tracer):
    """An ``fx.Tracer`` with custom granularity.

    This class is the blueprint for ``fx.Tracer``s that interpret user-defined
    ``nn.Module`` classes as atomic ``fx.Node``s during symbolic tracing.
    """

    def __init__(self,
                 leaf_types: Tuple[Type[nn.Module]],
                 *args,
                 **kwargs):

        super().__init__(*args, **kwargs)

        if len(leaf_types) == 0:
            raise ValueError(quantlib_err_header(obj_name=self.__class__.__name__) + "requires a non-empty list of `nn.Module`s to be treated as leaves.")
        self._leaf_types = leaf_types

    def is_leaf_module(self, m: nn.Module, module_qualified_name: str) -> bool:
        """Extend the base class check to custom ``nn.Module``s."""
        fxtracer_cond = super().is_leaf_module(m, module_qualified_name)
        custom_cond   = isinstance(m, self._leaf_types)
        return fxtracer_cond or custom_cond


class QModuleTracer(CustomTracer):

    def __init__(self, *args, **kwargs):
        super().__init__(leaf_types=(_QModule,), *args, **kwargs)


def custom_symbolic_trace(tracer: CustomTracer,
                          root: Union[Callable, nn.Module],
                          concrete_args: Union[None, Dict[str, Any]] = None) -> fx.GraphModule:

    graph = tracer.trace(root, concrete_args)
    name  = root.__class__.__name__ if isinstance(root, nn.Module) else root.__name__
    gm    = fx.GraphModule(tracer.root, graph, name)

    return gm


qmodule_symbolic_trace = partial(custom_symbolic_trace, tracer=QModuleTracer())
