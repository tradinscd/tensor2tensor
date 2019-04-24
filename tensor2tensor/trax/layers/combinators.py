# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Combinators for composing layers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensor2tensor.trax import backend
from tensor2tensor.trax.layers import base


class Serial(base.Layer):
  """Layer composing a number of sub-layers in a serial way.."""

  def __init__(self, *layers):
    super(Serial, self).__init__()
    self._nlayers = len(layers)
    self._layers = layers

  def call(self, x, params=(), **kwargs):
    rng = kwargs.pop('rng', None)
    rngs = (None,) * self._nlayers
    if rng is not None:
      rngs = backend.random.split(rng, self._nlayers)
    for layer, p, rng in zip(self._layers, params, rngs):
      x = layer(x, p, rng=rng, **kwargs)
    return x

  def output_shape(self, input_shape):
    cur_shape = input_shape
    for layer in self._layers:
      cur_shape = layer.output_shape(cur_shape)
    return cur_shape

  def new_parameters(self, input_shape, rng):
    params = []
    cur_shape = input_shape
    for layer in self._layers:
      rng, layer_rng = backend.random.split(rng)
      param = layer.initialize(cur_shape, layer_rng)
      cur_shape = layer.output_shape(cur_shape)
      params.append(param)
    return params


@base.layer()
def Identity(x, **unused_kwargs):
  """Identity layer, return the inputs."""
  return x


# Re-ordering layer.
def _reorder_shape(input_shape, output=None):  # pylint: disable=invalid-name
  """Helper to determine the shape of reorder output."""
  if output is None:
    return input_shape
  return base.nested_map(output, lambda i: input_shape[i])


@base.layer(output_shape=_reorder_shape)
def Reorder(x, params, output=None, **kwargs):
  """Reorder a tuple into another tuple.

  For example, we can re-order (x, y) into (y, x) or even (y, (x, y), y).
  The output argument specifies how to re-order, using integers that refer
  to indices in the input tuple. For example, if

    input = (x, y, z)

  then

    Reorder(input, output=(1, 0, 2))   = (y, x, z)
    Reorder(input, output=(0, 0))      = (x, x)
    Reorder(input, output=(0, (1, 1))) = (x, (y, y))
    Reorder(input, output=((2, 0), (1, 1))) = ((z, x), (y, y))

  By default (if no output is given) Reorder does nothing (Identity).

  Args:
    x: the input tuple to re-order.
    params: layer parameters (unused).
    output: the specification of the output tuple: a nested tuple of ints.
    **kwargs: other arguments (unused).

  Returns:
    The re-ordered tuple with the same shape as output.
  """
  del params, kwargs
  if output is None:
    return x
  return base.nested_map(output, lambda i: x[i])


@base.layer(output_shape=lambda shape, num_branches=2: [shape] * num_branches)
def Branch(x, params, num_branches=2, **kwargs):
  del params, kwargs
  return [x] * num_branches


@base.layer(output_shape=lambda input_shape_list: input_shape_list[0])
def FirstBranch(x, **unused_kwargs):
  return x[0]  # Here x is a list of tensors, we select the first.


@base.layer(output_shape=lambda input_shape_list: input_shape_list[1])
def SecondBranch(x, **unused_kwargs):
  return x[1]  # Here x is a list of tensors, we select the second.


@base.layer(output_shape=lambda input_shape_list: input_shape_list[0])
def SumBranches(x, **unused_kwargs):
  return sum(x)  # Here x is a list of tensors of the same shape, we add them.


def _concatenate_shape(input_shape, axis=-1):  # pylint: disable=invalid-name
  """Helper to determine the shape of Concatenate output."""
  ax = axis % len(input_shape[0])
  concat_size = sum(shape[ax] for shape in input_shape)
  out_shape = input_shape[0][:ax] + (concat_size,) + input_shape[0][ax+1:]
  return out_shape


@base.layer(output_shape=_concatenate_shape)
def Concatenate(x, params, axis=-1, **kwargs):
  del params, kwargs
  return backend.numpy.concatenate(x, axis)


class Parallel(base.Layer):
  """Combinator for composing layers in parallel.

  This layer is often used with the Branch and SumBranches layers.

  Args:
    *layers: a sequence of layers.

  Returns:
    A new layer representing parallel composition of the given layers.
    The new layer takes a sequence of inputs and returns a sequence of outputs
    with the same length as the argument `layers`.
  """

  def __init__(self, *layers):
    super(Parallel, self).__init__()
    self._nlayers = len(layers)
    self._layers = layers

  def call(self, inputs, params=(), **kwargs):
    rng = kwargs.pop('rng', None)
    rngs = (None,) * self._nlayers
    if rng is not None:
      rngs = backend.random.split(rng, self._nlayers)
    return [layer(x, params=p, rng=r, **kwargs)
            for layer, x, p, r in zip(self._layers, inputs, params, rngs)]

  def output_shape(self, input_shapes):
    return tuple([layer.output_shape(shape)
                  for layer, shape in zip(self._layers, input_shapes)])

  def new_parameters(self, input_shape, rng):
    rngs = backend.random.split(rng, self._nlayers)
    return [layer.initialize(shape, rng) for layer, shape, rng
            in zip(self._layers, input_shape, rngs)]


def Residual(*layers, **kwargs):
  """Constructs a residual version of layers, summing input to layers output."""
  shortcut = kwargs.get('shortcut', Identity())  # pylint: disable=no-value-for-parameter
  if len(layers) > 1:
    return Serial(
        Branch(),  # pylint: disable=no-value-for-parameter
        Parallel(Serial(*layers), shortcut),
        SumBranches()  # pylint: disable=no-value-for-parameter
    )
  elif len(layers) == 1:
    return Serial(
        Branch(),  # pylint: disable=no-value-for-parameter
        Parallel(layers[0], shortcut),
        SumBranches()  # pylint: disable=no-value-for-parameter
    )
  else:
    raise ValueError('Empty residual combinator.')
