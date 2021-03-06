from typing import *

from .. import tensor as T
from ..stochastic import StochasticTensor
from ..typing_ import *
from .base import Distribution
from .utils import log_pdf_mask, copy_distribution, check_tensor_arg_types

__all__ = ['Uniform']


class Uniform(Distribution):
    """Uniform distribution ``U[low, high)``."""

    continuous = True
    reparameterized = True
    min_event_ndims = 0

    _shape: Optional[List[int]]
    """The original `shape` argument for constructor."""

    low: T.Tensor
    """The lower-bound of the uniform distribution."""

    high: T.Tensor
    """The upper-bound of the uniform distribution (exclusive)."""

    log_zero: float

    _neg_log_high_minus_low: Optional[T.Tensor] = None
    """The cached computation result of log(high - low)."""

    def __init__(self,
                 shape: Optional[List[int]] = None,
                 low: Optional[TensorOrData] = None,
                 high: Optional[TensorOrData] = None,
                 dtype: str = T.float_x(),
                 reparameterized: bool = True,
                 event_ndims: int = 0,
                 device: Optional[str] = None,
                 log_zero: float = T.random.LOG_ZERO_VALUE,
                 validate_tensors: Optional[bool] = None):
        """
        Construct a new :class:`Uniform` distribution.

        Args:
            shape: Shape of the unit normal distribution, prepended to
                the front of ``get_broadcast_shape(low, high)``.  Defaults to `[]`.
            low: The lower-bound of the uniform distribution.
                If both `low` and `high` are not specified, the uniform
                distribution will be ``[0, 1)``.  Specifying only one of
                `low` or `high` is not allowed.
            high: The upper-bound of the uniform distribution (exclusive).
            dtype: Dtype of the samples.  Ignored if `low` and `high` are
                specified, and either of them is a tensor.
            reparameterized: Whether the distribution should be reparameterized?
            event_ndims: The number of dimensions in the samples to be
                considered as an event.
            device: The device where to place new tensors and variables.
            log_zero: The value to represent ``log(0)`` in the result of
                :meth:`log_prob()`, instead of using ``-math.inf``, to avoid
                potential numerical issues.
            validate_tensors: Whether or not to check the numerical issues?
                Defaults to ``settings.validate_tensors``.
        """
        if shape is not None:
            value_shape = shape = list(shape)
        else:
            value_shape = []

        if (low is None) != (high is None):
            raise ValueError('`low` and `high` must be both specified, or '
                             'neither specified.')

        range_checked = False
        if low is not None and high is not None:
            if isinstance(low, float) and isinstance(high, float):
                if low >= high:
                    raise ValueError(f'`low` < `high` does not hold: '
                                     f'`low` == {low}, `high` == {high}')
                range_checked = True

            low, high = check_tensor_arg_types(
                ('low', low), ('high', high), default_dtype=dtype,
                device=device,
            )

            dtype = T.get_dtype(low)
            value_shape = (value_shape +
                           T.get_broadcast_shape(T.shape(low), T.shape(high)))
            device = device or T.get_device(low)
        else:
            device = T.current_device()

        super().__init__(
            dtype=dtype,
            value_shape=value_shape,
            reparameterized=reparameterized,
            event_ndims=event_ndims,
            device=device,
            validate_tensors=validate_tensors,
        )

        if self.validate_tensors and low is not None and high is not None and \
                not range_checked:
            if not T.is_all(T.less(low, high)):
                raise ValueError('`low` < `high` does not hold.')

        self._shape = shape
        self.low = low
        self.high = high
        self.log_zero = log_zero

    def _get_neg_log_high_minus_low(self) -> T.Tensor:
        if self._neg_log_high_minus_low is None:
            if self.low is None or self.high is None:
                self._neg_log_high_minus_low = T.zeros([], dtype=self.dtype)
            else:
                self._neg_log_high_minus_low = -T.log(self.high - self.low)
        return self._neg_log_high_minus_low

    def _sample(self,
                n_samples: Optional[int],
                group_ndims: int,
                reduce_ndims: int,
                reparameterized: bool) -> StochasticTensor:
        sample_shape = ([n_samples] + self.value_shape if n_samples is not None
                        else self.value_shape)
        samples = T.random.rand(sample_shape, dtype=self.dtype, device=self.device)
        if self.low is not None and self.high is not None:
            scale = self.high - self.low
            samples = samples * scale + self.low
        if not reparameterized:
            samples = T.stop_grad(samples)

        return StochasticTensor(
            tensor=samples,
            distribution=self,
            n_samples=n_samples,
            group_ndims=group_ndims,
            reparameterized=reparameterized,
        )

    def _log_prob(self,
                  given: T.Tensor,
                  group_ndims: int,
                  reduce_ndims: int) -> T.Tensor:
        low = self.low if self.low is not None else 0.
        high = self.high if self.high is not None else 1.
        log_pdf = self._get_neg_log_high_minus_low()
        log_pdf = log_pdf_mask(
            T.logical_and(low <= given, given <= high),
            log_pdf,
            self.log_zero,
        )

        # broadcast against given if required
        log_pdf = T.broadcast_to(log_pdf, given)
        if reduce_ndims > 0:
            log_pdf = T.reduce_sum(log_pdf, axis=list(range(-reduce_ndims, 0)))
        return log_pdf

    def copy(self, **overrided_params):
        return copy_distribution(
            cls=Uniform,
            base=self,
            attrs=(('shape', '_shape'), 'low', 'high', 'dtype',
                   'reparameterized', 'event_ndims', 'log_zero',
                   'device', 'validate_tensors'),
            overrided_params=overrided_params,
        )
