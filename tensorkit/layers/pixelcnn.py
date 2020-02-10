from functools import partial
from typing import *

from . import resnet, core, composed
from .core import *
from .utils import flatten_nested_layers
from .. import tensor as T
from ..arg_check import *
from ..tensor import Tensor, Module, rank, shift, shape, concat, ones_like
from ..typing_ import *

__all__ = [
    'PixelCNNInput1d', 'PixelCNNInput2d', 'PixelCNNInput3d',
    'PixelCNNOutput1d', 'PixelCNNOutput2d', 'PixelCNNOutput3d',
    'PixelCNNResBlock1d', 'PixelCNNResBlock2d', 'PixelCNNResBlock3d',
    'PixelCNN1d', 'PixelCNN2d', 'PixelCNN3d',
]


def shifted_conv(conv_cls,
                 in_channels: int,
                 out_channels: int,
                 spatial_shift: Sequence[bool],
                 kernel_size: Union[int, Sequence[int]],
                 dilation: Union[int, Sequence[int]] = 1,
                 **kwargs):
    kwargs.pop('padding', None)  # NOTE: here ignore any given padding.

    spatial_shift = list(spatial_shift)
    spatial_ndims = len(spatial_shift)
    kernel_size = validate_conv_size('kernel_size', kernel_size, spatial_ndims)
    dilation = validate_conv_size('dilation', dilation, spatial_ndims)

    padding = []
    for shift, k, d in zip(spatial_shift, kernel_size, dilation):
        t = (k - 1) * d
        if shift:
            padding.append((t, 0))
        else:
            padding.append((t // 2, t - t // 2))

    return conv_cls(in_channels, out_channels, kernel_size=kernel_size,
                    dilation=dilation, padding=padding, **kwargs)


class SpatialShift(BaseSingleVariateLayer):

    __constants__ = ('shift',)

    shift: List[int]

    def __init__(self, shift: Sequence[int]):
        super().__init__()
        if T.IS_CHANNEL_LAST:
            self.shift = list(shift) + [0]
        else:
            self.shift = list(shift)

    def _call(self, input: Tensor) -> Tensor:
        return shift(input, self.shift)


class BranchAndAdd(BaseSingleVariateLayer):

    __constants__ = ('branches',)

    branches: ModuleList

    def __init__(self, *branches: Union[Module, Sequence[Module]]):
        super().__init__()
        self.branches = ModuleList(flatten_nested_layers(branches))

    def _call(self, input: Tensor) -> Tensor:
        branch_outputs: List[Tensor] = []
        for branch in self.branches:
            branch_outputs.append(branch(input))
        output = branch_outputs[0]
        for branch_output in branch_outputs[1:]:
            output = output + branch_output
        return output


class AddOnesChannelNd(BaseSingleVariateLayer):

    __constants__ = ('_channel_axis', '_spatial_ndims')

    _channel_axis: int
    _spatial_ndims: int

    def __init__(self):
        super().__init__()
        spatial_ndims = self._get_spatial_ndims()
        self._spatial_ndims = spatial_ndims
        if T.IS_CHANNEL_LAST:
            self._channel_axis = -1
        else:
            self._channel_axis = -(spatial_ndims + 1)

    def _get_spatial_ndims(self) -> int:
        raise NotImplementedError()

    def _call(self, input: Tensor) -> Tensor:
        channel_shape = shape(input)
        channel_shape[self._channel_axis] = 1

        return concat([input, ones_like(input, shape=channel_shape)],
                      axis=self._channel_axis)


class AddOnesChannel1d(AddOnesChannelNd):

    def _get_spatial_ndims(self) -> int:
        return 1


class AddOnesChannel2d(AddOnesChannelNd):

    def _get_spatial_ndims(self) -> int:
        return 2


class AddOnesChannel3d(AddOnesChannelNd):

    def _get_spatial_ndims(self) -> int:
        return 3


class AddLeadingContext(BaseContextualLayer):

    __constants__ = ('first_n',)

    def __init__(self, first_n: int):
        super().__init__()
        self.first_n = first_n

    def _call(self, input: Tensor, context: List[Tensor]) -> Tensor:
        output = input
        for i in range(self.first_n):
            output = output + context[i]
        return output


class IgnoreLeadingContext(BaseContextualLayer):

    __constants__ = ('wrapped', 'first_n',)

    wrapped: Module
    first_n: int

    def __init__(self, wrapped: Module, first_n: int):
        super().__init__()
        self.wrapped = wrapped
        self.first_n = first_n

    def _call(self, input: Tensor, context: List[Tensor]) -> Tensor:
        return self.wrapped(input, context[self.first_n:])


def get_stack_kernel_sizes(kernel_size: List[int]) -> List[List[int]]:
    spatial_ndims = len(kernel_size)
    ret = []
    for i in range(spatial_ndims):
        t = []
        for j in range(spatial_ndims):
            if j <= i:
                k_size = (kernel_size[j] + 1) // 2
            else:
                k_size = kernel_size[j]
            t.append(k_size)
        ret.append(t)
    return ret


def get_stack_conv_shifts(spatial_ndims: int) -> List[List[bool]]:
    ret = []
    for i in range(spatial_ndims):
        ret.append([True] * (i + 1) + [False] * (spatial_ndims - i - 1))
    return ret


# ---- pixelcnn input layer, which constructs the multiple pixelcnn stacks ----
class PixelCNNInputNd(BaseSplitLayer):

    __constants__ = ('_spatial_ndims', 'add_ones_channel', 'stacks',)

    _spatial_ndims: int
    add_ones_channel: Module
    stacks: ModuleList

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 kernel_size: Union[int, Sequence[int]],
                 add_ones_channel: bool = True,
                 weight_norm: WeightNormArgType = False,
                 weight_init: TensorInitArgType = DEFAULT_WEIGHT_INIT,
                 bias_init: TensorInitArgType = DEFAULT_BIAS_INIT,
                 data_init: Optional[DataInitArgType] = None):
        """
        Construct a new pixelcnn input layer.

        Args:
            in_channels: The number of channels of the input.
            out_channels: The number of channels of the output.
            kernel_size: The "full" kernel size, which is the idealistic
                kernel size before applying PixelCNN kernel masks.

                The actual kernel size used by the convolutional layers
                will be re-calculated under the guide of this kernel size,
                in order to ensure causality between pixels.
            add_ones_channel: Whether or not add a channel to the input,
                with all elements set to `1`?
            weight_norm: The weight norm mode for the convolutional layers.
                If :obj:`True`, will use "full" weight norm for "conv1" and
                "shortcut".  For "conv0", will use "full" if `normalizer`
                is :obj:`None` or `dropout` is not :obj:`None`.

                If :obj:`False`, will not use weight norm for all layers.
            weight_init: The weight initializer for the convolutional layers.
            bias_init: The bias initializer for the convolutional layers.
            data_init: The data-dependent initializer for the convolutional layers.
        """
        super().__init__()

        globals_dict = globals()
        spatial_ndims = self._get_spatial_ndims()
        kernel_size = validate_conv_size('kernel_size', kernel_size, spatial_ndims)
        
        # construct the layer
        super().__init__()
        self._spatial_ndims = spatial_ndims
        
        if add_ones_channel:
            self.add_ones_channel = globals_dict[f'AddOnesChannel{spatial_ndims}d']()
            in_channels += 1
        else:
            self.add_ones_channel = Identity()

        stack_kernel_sizes = get_stack_kernel_sizes(kernel_size)
        stack_conv_shifts = get_stack_conv_shifts(spatial_ndims)

        stacks = []
        for i in range(1, spatial_ndims + 1):
            stack_branches = []
            for j in range(i):
                spatial_shift = [0] * spatial_ndims
                spatial_shift[j] = 1

                # architecture similar to PixelCNN++, but the kernel_size varies.
                stack_branches.append(
                    Sequential(
                        shifted_conv(
                            getattr(composed, f'Conv{spatial_ndims}d'),
                            in_channels=in_channels,
                            out_channels=out_channels,
                            spatial_shift=stack_conv_shifts[j],
                            kernel_size=stack_kernel_sizes[j],
                            weight_norm=weight_norm,
                            weight_init=weight_init,
                            bias_init=bias_init,
                            data_init=data_init,
                        ),
                        SpatialShift(spatial_shift)
                    )
                )

            if len(stack_branches) == 1:
                stacks.append(stack_branches[0])
            else:
                stacks.append(BranchAndAdd(stack_branches))
        self.stacks = ModuleList(stacks)

    def _get_spatial_ndims(self) -> int:
        raise NotImplementedError()

    def _call(self, input: Tensor) -> List[Tensor]:
        if rank(input) != self._spatial_ndims + 2:
            raise ValueError(
                '`input` is expected to be {}d: got input shape {}.'.
                format(self._spatial_ndims + 2, rank(input))
            )

        output = self.add_ones_channel(input)
        outputs: List[Tensor] = []
        for stack in self.stacks:
            outputs.append(stack(output))
        return outputs


class PixelCNNInput1d(PixelCNNInputNd):
    """
    Prepare the input for a PixelCNN 1D network.

    This layer must be first layer of a PixelCNN 1D network, before any PixelCNN
    layers like :class:`PixelCNNResBlock1d`.
    """

    def _get_spatial_ndims(self) -> int:
        return 1


class PixelCNNInput2d(PixelCNNInputNd):
    """
    Prepare the input for a PixelCNN 2D network.

    This layer must be first layer of a PixelCNN 2D network, before any PixelCNN
    layers like :class:`PixelCNNResBlock2d`.
    """

    def _get_spatial_ndims(self) -> int:
        return 2


class PixelCNNInput3d(PixelCNNInputNd):
    """
    Prepare the input for a PixelCNN 3D network.

    This layer must be first layer of a PixelCNN 3D network, before any PixelCNN
    layers like :class:`PixelCNNResBlock3d`.
    """

    def _get_spatial_ndims(self) -> int:
        return 3


# ---- pixelcnn output layer, which obtains the final output from the stacks ----
class PixelCNNOutputNd(BaseMergeLayer):

    __constants__ = ('_spatial_ndims',)

    _spatial_ndims: int

    def __init__(self):
        super().__init__()
        self._spatial_ndims = self._get_spatial_ndims()

    def _get_spatial_ndims(self) -> int:
        raise NotImplementedError()

    def _call(self, inputs: List[Tensor]) -> Tensor:
        if len(inputs) != self._spatial_ndims:
            raise ValueError(
                '`len(inputs)` is expected to be {}: got {} tensors.'.
                format(self._spatial_ndims, len(inputs))
            )
        return inputs[-1]


class PixelCNNOutput1d(PixelCNNOutputNd):
    """
    Extract the final output from 1D PixelCNN layers.
    The output of the last PixelCNN stack should be the final output.
    """

    def _get_spatial_ndims(self) -> int:
        return 1


class PixelCNNOutput2d(PixelCNNOutputNd):
    """
    Extract the final output from 2D PixelCNN layers.
    The output of the last PixelCNN stack should be the final output.
    """

    def _get_spatial_ndims(self) -> int:
        return 2


class PixelCNNOutput3d(PixelCNNOutputNd):
    """
    Extract the final output from 3D PixelCNN layers.
    The output of the last PixelCNN stack should be the final output.
    """

    def _get_spatial_ndims(self) -> int:
        return 3


# ---- pixelcnn layers ----
class PixelCNNResBlockNd(BaseMultiVariateContextualLayer):

    __constants__ = ('_spatial_ndims', 'resnet_layers',)

    _spatial_ndims: int

    resnet_layers: ModuleList
    """The resnet layers for each PixelCNN stack."""

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 kernel_size: Union[int, Sequence[int]] = 3,
                 merge_context1: Optional[LayerFactory] = None,
                 activation: Optional[LayerFactory] = None,
                 normalizer: Optional[NormalizerFactory] = None,
                 dropout: Optional[Union[float, LayerFactory]] = None,
                 weight_norm: WeightNormArgType = False,
                 gated: bool = False,
                 gate_bias: float = DEFAULT_GATE_BIAS,
                 weight_init: TensorInitArgType = DEFAULT_WEIGHT_INIT,
                 bias_init: TensorInitArgType = DEFAULT_BIAS_INIT,
                 data_init: Optional[DataInitArgType] = None,
                 ):
        """
        Construct a new PixelCNN resnet block.

        Args:
            in_channels: The number of channels of the input.
            out_channels: The number of channels of the output.
            kernel_size: The "full" kernel size, which is the idealistic
                kernel size before applying PixelCNN kernel masks.

                The actual kernel size used by the convolutional layers
                will be re-calculated under the guide of this kernel size,
                in order to ensure causality between pixels.
            merge_context1: Factory to create the layer after "conv1" of the
                resnet blocks, which merge thes `context` argument with the
                output of "conv1".
            activation: The factory of the activation layers.
                It should expect no argument.
            normalizer: The factory of the normalizer layers.  It should accept
                one positional argument, the output channel size.
            dropout: A float, a layer or a factory.
                If it is a float, it will be used as the `p` argument to
                construct an instance of :class:`tensorkit.layers.Dropout`.
                If it is a factory, it should expect no argument.
            weight_norm: The weight norm mode for the convolutional layers.
                If :obj:`True`, will use "full" weight norm for "conv1" and
                "shortcut".  For "conv0", will use "full" if `normalizer`
                is :obj:`None` or `dropout` is not :obj:`None`.

                If :obj:`False`, will not use weight norm for all layers.
            gated: Whether or not to use gate on the output of "conv1"?
                `conv1 = activation(conv1) * sigmoid(gate + gate_bias)`.
            gate_bias: The bias added to `gate` before applying the `sigmoid`
                activation.
            weight_init: The weight initializer for the convolutional layers.
            bias_init: The bias initializer for the convolutional layers.
            data_init: The data-dependent initializer for the convolutional layers.
        """
        spatial_ndims = self._get_spatial_ndims()

        # validate the arguments
        kernel_size = validate_conv_size('kernel_size', kernel_size, spatial_ndims)

        for k in kernel_size:
            if k < 3:
                raise ValueError(
                    f'`kernel_size` is required to be at least 3: got '
                    f'kernel_size {kernel_size}.'
                )
            if k % 2 != 1:
                raise ValueError(
                    f'`kernel_size` is required to be odd: got kernel_size '
                    f'{kernel_size}.'
                )

        if merge_context1 is not None:
            merge_context1 = validate_layer_factory('merge_context1', merge_context1)
        if activation is not None:
            activation = validate_layer_factory('activation', activation)
        if normalizer is not None:
            normalizer = validate_layer_factory('normalizer', normalizer)
        if dropout is not None:
            if not isinstance(dropout, float):
                dropout = validate_layer_factory('dropout', dropout)

        # construct the pixelcnn layer stacks
        resnet_layers = []
        stack_kernel_sizes = get_stack_kernel_sizes(kernel_size)
        stack_conv_shifts = get_stack_conv_shifts(spatial_ndims)

        for i in range(spatial_ndims):
            # the resnet layer
            if i > 0:
                this_merge_context0 = AddLeadingContext(i)
                if merge_context1 is not None:
                    this_merge_context1 = IgnoreLeadingContext(merge_context1(), i)
                else:
                    this_merge_context1 = None
            else:
                this_merge_context0 = None
                if merge_context1 is not None:
                    this_merge_context1 = merge_context1()
                else:
                    this_merge_context1 = None

            conv_factory = partial(
                shifted_conv,
                getattr(core, f'LinearConv{spatial_ndims}d'),
                spatial_shift=stack_conv_shifts[i],
            )
            resnet_layers.append(
                getattr(resnet, f'ResBlock{spatial_ndims}d')(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    kernel_size=stack_kernel_sizes[i],
                    stride=1,
                    resize_at_exit=False,
                    shortcut=conv_factory,
                    conv0=conv_factory,
                    conv1=conv_factory,
                    merge_context0=this_merge_context0,
                    merge_context1=this_merge_context1,
                    activation=activation,
                    normalizer=normalizer,
                    dropout=dropout,
                    weight_norm=weight_norm,
                    gated=gated,
                    gate_bias=gate_bias,
                    weight_init=weight_init,
                    bias_init=bias_init,
                    data_init=data_init,
                )
            )

        super().__init__()

        self._spatial_ndims = spatial_ndims
        self.resnet_layers = ModuleList(resnet_layers)

    def _call(self, inputs: List[Tensor], context: List[Tensor]) -> List[Tensor]:
        resnet_outputs: List[Tensor] = []
        i = 0
        for resnet_layer in self.resnet_layers:
            this_output = resnet_layer(
                inputs[i],
                resnet_outputs + context,
            )
            resnet_outputs.append(this_output)
            i += 1

        return resnet_outputs


class PixelCNNResBlock1d(PixelCNNResBlockNd):
    """PixelCNN 1D ResNet block."""

    def _get_spatial_ndims(self) -> int:
        return 1


class PixelCNNResBlock2d(PixelCNNResBlockNd):
    """PixelCNN 2D ResNet block."""

    def _get_spatial_ndims(self) -> int:
        return 2


class PixelCNNResBlock3d(PixelCNNResBlockNd):
    """PixelCNN 3D ResNet block."""

    def _get_spatial_ndims(self) -> int:
        return 3


# ---- pixelcnn composite blocks ----
class PixelCNNNd(BaseContextualLayer):

    __constants__ = ('input_layer', 'layers', 'output_layer')

    input_layer: Module
    layers: ModuleList
    output_layer: Module

    def __init__(self,
                 input_layer: Module,
                 *layers: Union[Module, Sequence[Module]]):
        """
        Construct the PixelCNN network.

        Args:
            input_layer: The input layer.
            *layers: The convolution layers.
        """
        spatial_ndims = self._get_spatial_ndims()
        global_dict = globals()

        input_cls_name = f'PixelCNNInput{spatial_ndims}d'
        if not isinstance(input_layer, global_dict[input_cls_name]) and \
                not T.is_jit_layer(input_layer):
            raise TypeError(
                f'`input_layer` must be an instance of `{input_cls_name}`: '
                f'got {input_layer!r}.'
            )
        layers = flatten_nested_layers(layers)

        output_layer_cls = global_dict[f'PixelCNNOutput{spatial_ndims}d']

        super().__init__()
        self.input_layer = input_layer
        self.layers = ModuleList(layers)
        self.output_layer = output_layer_cls()

    def _get_spatial_ndims(self) -> int:
        raise NotImplementedError()

    def _call(self, input: Tensor, context: List[Tensor]) -> Tensor:
        outputs = self.input_layer(input)
        for block in self.layers:
            outputs = block(outputs, context)
        return self.output_layer(outputs)


class PixelCNN1d(PixelCNNNd):
    """
    PixelCNN 1D network.

    This layer composes the input layer, the convolution layers, and the
    output layer of the PixelCNN network as a whole layer.
    The composed layer can be used as an ordinary layer, with single-input and
    single-output.
    """

    def _get_spatial_ndims(self) -> int:
        return 1


class PixelCNN2d(PixelCNNNd):
    """
    PixelCNN 2D network.

    This layer composes the input layer, the convolution layers, and the
    output layer of the PixelCNN network as a whole layer.
    The composed layer can be used as an ordinary layer, with single-input and
    single-output.
    """

    def _get_spatial_ndims(self) -> int:
        return 2


class PixelCNN3d(PixelCNNNd):
    """
    PixelCNN 3D network.

    This layer composes the input layer, the convolution layers, and the
    output layer of the PixelCNN network as a whole layer.
    The composed layer can be used as an ordinary layer, with single-input and
    single-output.
    """

    def _get_spatial_ndims(self) -> int:
        return 3
