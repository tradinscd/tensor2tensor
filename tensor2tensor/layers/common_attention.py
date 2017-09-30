# coding=utf-8
# Copyright 2017 The Tensor2Tensor Authors.
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

"""Utilities for attention."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import math

# Dependency imports
import numpy as np

from six.moves import range  # pylint: disable=redefined-builtin
from six.moves import xrange  # pylint: disable=redefined-builtin
from six.moves import zip  # pylint: disable=redefined-builtin

from tensor2tensor.layers import common_layers
from tensor2tensor.utils import expert_utils

import tensorflow as tf

from tensorflow.python.framework import function


_expert_count = 0


def get_timing_signal_1d(
    length, channels, min_timescale=1.0, max_timescale=1.0e4):
  """Gets a bunch of sinusoids of different frequencies.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(x+y) and cos(x+y) can be
  experessed in terms of y, sin(x) and cos(x).

  In particular, we use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels / 2. For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    length: scalar, length of timing signal sequence.
    channels: scalar, size of timing embeddings to create. The number of
        different timescales is equal to channels / 2.
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor of timing signals [1, length, channels]
  """
  position = tf.to_float(tf.range(length))
  num_timescales = channels // 2
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (tf.to_float(num_timescales) - 1))
  inv_timescales = min_timescale * tf.exp(
      tf.to_float(tf.range(num_timescales)) * -log_timescale_increment)
  scaled_time = tf.expand_dims(position, 1) * tf.expand_dims(inv_timescales, 0)
  signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=1)
  signal = tf.pad(signal, [[0, 0], [0, tf.mod(channels, 2)]])
  signal = tf.reshape(signal, [1, length, channels])
  return signal


def add_timing_signal_1d(x, min_timescale=1.0, max_timescale=1.0e4):
  """Adds a bunch of sinusoids of different frequencies to a Tensor.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(x+y) and cos(x+y) can be
  experessed in terms of y, sin(x) and cos(x).

  In particular, we use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels / 2. For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    x: a Tensor with shape [batch, length, channels]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  length = tf.shape(x)[1]
  channels = tf.shape(x)[2]
  signal = get_timing_signal_1d(length, channels, min_timescale, max_timescale)
  return x + signal


def add_timing_signal_1d_given_position(x, position, min_timescale=1.0,
                                        max_timescale=1.0e4):
  """Adds sinusoids of diff frequencies to a Tensor, with timing position given.

  Args:
    x: a Tensor with shape [batch, length, channels]
    position: a Tensor with shape [batch, length]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  channels = tf.shape(x)[2]
  num_timescales = channels // 2
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (tf.to_float(num_timescales) - 1))
  inv_timescales = min_timescale * tf.exp(
      tf.to_float(tf.range(num_timescales)) * -log_timescale_increment)
  scaled_time = (tf.expand_dims(tf.to_float(position), 2) *
                 tf.expand_dims(tf.expand_dims(inv_timescales, 0), 0))
  signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=2)
  signal = tf.pad(signal, [[0, 0], [0, 0], [0, tf.mod(channels, 2)]])
  return x + signal


def add_timing_signal_nd(x, min_timescale=1.0, max_timescale=1.0e4):
  """Adds a bunch of sinusoids of different frequencies to a Tensor.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase in one of the positional dimensions.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(a+b) and cos(a+b) can be
  experessed in terms of b, sin(a) and cos(a).

  x is a Tensor with n "positional" dimensions, e.g. one dimension for a
  sequence or two dimensions for an image

  We use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels // (n * 2). For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    x: a Tensor with shape [batch, d1 ... dn, channels]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  static_shape = x.get_shape().as_list()
  num_dims = len(static_shape) - 2
  channels = tf.shape(x)[-1]
  num_timescales = channels // (num_dims * 2)
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (tf.to_float(num_timescales) - 1))
  inv_timescales = min_timescale * tf.exp(
      tf.to_float(tf.range(num_timescales)) * -log_timescale_increment)
  for dim in xrange(num_dims):
    length = tf.shape(x)[dim + 1]
    position = tf.to_float(tf.range(length))
    scaled_time = tf.expand_dims(position, 1) * tf.expand_dims(
        inv_timescales, 0)
    signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=1)
    prepad = dim * 2 * num_timescales
    postpad = channels - (dim + 1) * 2 * num_timescales
    signal = tf.pad(signal, [[0, 0], [prepad, postpad]])
    for _ in xrange(1 + dim):
      signal = tf.expand_dims(signal, 0)
    for _ in xrange(num_dims - 1 - dim):
      signal = tf.expand_dims(signal, -2)
    x += signal
  return x


def add_positional_embedding_nd(x, max_length, name):
  """Add n-dimensional positional embedding.

  Adds embeddings to represent the positional dimensions of the tensor.
  The input tensor has n positional dimensions - i.e. 1 for text, 2 for images,
  3 for video, etc.

  Args:
    x: a Tensor with shape [batch, p1 ... pn, depth]
    max_length: an integer.  static maximum size of any dimension.
    name: a name for this layer.

  Returns:
    a Tensor the same shape as x.
  """
  static_shape = x.get_shape().as_list()
  dynamic_shape = tf.shape(x)
  num_dims = len(static_shape) - 2
  depth = static_shape[-1]
  base_shape = [1] * (num_dims + 1) + [depth]
  base_start = [0] * (num_dims + 2)
  base_size = [-1] + [1] * num_dims + [depth]
  for i in xrange(num_dims):
    shape = base_shape[:]
    start = base_start[:]
    size = base_size[:]
    shape[i + 1] = max_length
    size[i + 1] = dynamic_shape[i + 1]
    var = (tf.get_variable(
        name + "_%d" % i,
        shape,
        initializer=tf.random_normal_initializer(0, depth**-0.5)) *
           (depth**0.5))
    x += tf.slice(var, start, size)
  return x


def embedding_to_padding(emb):
  """Calculates the padding mask based on which embeddings are all zero.

  We have hacked symbol_modality to return all-zero embeddings for padding.

  Args:
    emb: a Tensor with shape [..., depth].
  Returns:
    a float Tensor with shape [...].
  """
  emb_sum = tf.reduce_sum(tf.abs(emb), axis=-1)
  return tf.to_float(tf.equal(emb_sum, 0.0))


def attention_bias_local(length, max_backward, max_forward):
  """Create an bias tensor to be added to attention logits.

  A position may attend to positions at most max_distance from it,
  forward and backwards.

  This does not actually save any computation.

  Args:
    length: an integer Scalar.
    max_backward: an int64 Scalar - maximum distance backward to attend.
      negative values indicate unlimited.
    max_forward: an int64 Scalar - maximum distance forward to attend.
      negative values indicate unlimited.

  Returns:
    a `Tensor` with shape [1, 1, length, length].
  """
  band = tf.matrix_band_part(
      tf.ones([length, length]), max_backward, max_forward)
  ret = -1e9 * (1.0 - band)
  return tf.reshape(ret, [1, 1, length, length])


def attention_bias_lower_triangle(length):
  """Create an bias tensor to be added to attention logits.

  Allows a query to attend to all positions up to and including its own.

  Args:
   length: a Scalar.

  Returns:
    a `Tensor` with shape [1, 1, length, length].
  """
  return attention_bias_local(length, -1, 0)


def attention_bias_ignore_padding(memory_padding):
  """Create an bias tensor to be added to attention logits.

  Args:
    memory_padding: a float `Tensor` with shape [batch, memory_length].

  Returns:
    a `Tensor` with shape [batch, 1, 1, memory_length].
  """
  ret = memory_padding * -1e9
  return tf.expand_dims(tf.expand_dims(ret, axis=1), axis=1)


def attention_bias_to_padding(attention_bias):
  """Inverse of attention_bias_ignore_padding().

  Args:
    attention_bias: a `Tensor` with shape [batch, 1, 1, memory_length], as
      returned by attention_bias_ignore_padding().

  Returns:
    a Tensor with shape [batch, memory_length] with 1.0 in padding positions
    and 0.0 in non-padding positions.
  """
  # `attention_bias` is a large negative number in padding positions and 0.0
  # elsewhere.
  return tf.squeeze(tf.to_float(tf.less(attention_bias, -1)), axis=[1, 2])


def attention_bias_prepend_inputs_full_attention(padding):
  """Create a bias tensor for prepend_mode="prepend_inputs_full_attention".

  See prepend_inputs in common_hparams.py.

  Produces a bias tensor to be used in self-attention.

  This bias tensor allows for full connectivity in the "inputs" part of
  the sequence and masked connectivity in the targets part.

  Args:
    padding: a float `Tensor` with shape [batch, length] with
      ones in positions corresponding to padding.  In each row, a single
      padding position separates the input part from the target part.

  Returns:
    a `Tensor` with shape [batch, 1, length, length].
  """
  # Everything past the first padding position is part of the target.
  # This Tensor has zeros for the source portion and separator,
  # and ones for the target portion.
  in_target = tf.cumsum(padding, axis=1, exclusive=True)
  # The position within the target, or 0 if part of the source.
  target_pos = tf.cumsum(in_target, axis=1)
  # A position with a lesser target_pos cannot see a position with greater
  # target_pos.
  illegal_connections = tf.greater(tf.expand_dims(target_pos, 1),
                                   tf.expand_dims(target_pos, 2))
  bias = tf.to_float(illegal_connections) * -1e9
  bias = tf.expand_dims(bias, 1)
  return bias


def attention_bias_proximal(length):
  """Bias for self-attention to encourage attention to close positions.

  Args:
    length: an integer scalar.

  Returns:
    a Tensor with shape [1, 1, length, length]
  """
  r = tf.to_float(tf.range(length))
  diff = tf.expand_dims(r, 0) - tf.expand_dims(r, 1)
  return tf.expand_dims(tf.expand_dims(-tf.log(1 + tf.abs(diff)), 0), 0)


@expert_utils.add_name_scope()
def attention_bias_coordinates(batch_coordinate):
  """Generate a mask to prevent the batch to attend to each others.

  Args:
    batch_coordinate (tf.Tensor): int32 of shape [length, 1] containing the
      coordinates of the batches

  Returns:
    tf.Tensor: float32 mask of shape [length, length] containing either 0 or
      -infinity (-1e9)
  """
  batch_coord_float = tf.squeeze(batch_coordinate, 1)
  # Convert to float first because of b/25387198
  batch_coord_float = tf.to_float(batch_coord_float)
  bc_v = tf.expand_dims(batch_coord_float, 1)
  bc_h = tf.expand_dims(batch_coord_float, 0)
  bias_batch = bc_v - bc_h  # Broadcast to create [length, length] mask
  # Theshold non zeros to 1.0
  bias_batch = tf.minimum(1.0, tf.abs(bias_batch))
  bias_batch *= -1e9  # Set non zeros to -infinity
  return bias_batch


def split_last_dimension(x, n):
  """Reshape x so that the last dimension becomes two dimensions.

  The first of these two dimensions is n.

  Args:
    x: a Tensor with shape [..., m]
    n: an integer.

  Returns:
    a Tensor with shape [..., n, m/n]
  """
  old_shape = x.get_shape().dims
  last = old_shape[-1]
  new_shape = old_shape[:-1] + [n] + [last // n if last else None]
  ret = tf.reshape(x, tf.concat([tf.shape(x)[:-1], [n, -1]], 0))
  ret.set_shape(new_shape)
  return ret


def combine_last_two_dimensions(x):
  """Reshape x so that the last two dimension become one.

  Args:
    x: a Tensor with shape [..., a, b]

  Returns:
    a Tensor with shape [..., ab]
  """
  old_shape = x.get_shape().dims
  a, b = old_shape[-2:]
  new_shape = old_shape[:-2] + [a * b if a and b else None]
  ret = tf.reshape(x, tf.concat([tf.shape(x)[:-2], [-1]], 0))
  ret.set_shape(new_shape)
  return ret


def combine_first_two_dimensions(x):
  """Reshape x so that the first two dimension become one.

  Args:
    x: a Tensor with shape [a, b, ...]

  Returns:
    a Tensor with shape [ab, ...]
  """
  ret = tf.reshape(x, tf.concat([[-1], tf.shape(x)[2:]], 0))
  old_shape = x.get_shape().dims
  a, b = old_shape[:2]
  new_shape = [a * b if a and b else None] + old_shape[2:]
  ret.set_shape(new_shape)
  return ret


def split_heads(x, num_heads):
  """Split channels (dimension 3) into multiple heads (becomes dimension 1).

  Args:
    x: a Tensor with shape [batch, length, channels]
    num_heads: an integer

  Returns:
    a Tensor with shape [batch, num_heads, length, channels / num_heads]
  """
  return tf.transpose(split_last_dimension(x, num_heads), [0, 2, 1, 3])


def split_heads_2d(x, num_heads):
  """Split channels (dimension 4) into multiple heads (becomes dimension 1).

  Args:
    x: a Tensor with shape [batch, height, width, channels]
    num_heads: an integer

  Returns:
    a Tensor with shape [batch, num_heads, height, width, channels / num_heads]
  """
  return tf.transpose(split_last_dimension(x, num_heads), [0, 3, 1, 2, 4])


def combine_heads(x):
  """Inverse of split_heads.

  Args:
    x: a Tensor with shape [batch, num_heads, length, channels / num_heads]

  Returns:
    a Tensor with shape [batch, length, channels]
  """
  return combine_last_two_dimensions(tf.transpose(x, [0, 2, 1, 3]))


def combine_heads_2d(x):
  """Inverse of split_heads_2d.

  Args:
    x: a Tensor with shape
      [batch, num_heads, height, width, channels / num_heads]

  Returns:
    a Tensor with shape [batch, height, width, channels]
  """
  return combine_last_two_dimensions(tf.transpose(x, [0, 2, 3, 1, 4]))


def attention_image_summary(attn, image_shapes=None):
  """Compute color image summary.

  Args:
    attn: a Tensor with shape [batch, num_heads, query_length, memory_length]
    image_shapes: optional tuple of integer scalars.
      If the query positions and memory positions represent the
      pixels of flattened images, then pass in their dimensions:
        (query_rows, query_cols, memory_rows, memory_cols).
      If the query positions and memory positions represent the
      pixels x channels of flattened images, then pass in their dimensions:
        (query_rows, query_cols, query_channels,
         memory_rows, memory_cols, memory_channels).
  """
  num_heads = tf.shape(attn)[1]
  # [batch, query_length, memory_length, num_heads]
  image = tf.transpose(attn, [0, 2, 3, 1])
  image = tf.pow(image, 0.2)  # for high-dynamic-range
  # Each head will correspond to one of RGB.
  # pad the heads to be a multiple of 3
  image = tf.pad(image, [[0, 0], [0, 0], [0, 0], [0, tf.mod(-num_heads, 3)]])
  image = split_last_dimension(image, 3)
  image = tf.reduce_max(image, 4)
  if image_shapes is not None:
    if len(image_shapes) == 4:
      q_rows, q_cols, m_rows, m_cols = list(image_shapes)
      image = tf.reshape(image, [-1, q_rows, q_cols, m_rows, m_cols, 3])
      image = tf.transpose(image, [0, 1, 3, 2, 4, 5])
      image = tf.reshape(image, [-1, q_rows * m_rows, q_cols * m_cols, 3])
    else:
      assert len(image_shapes) == 6
      q_rows, q_cols, q_channnels, m_rows, m_cols, m_channels = list(
          image_shapes)
      image = tf.reshape(image, [
          -1, q_rows, q_cols, q_channnels, m_rows, m_cols, m_channels, 3
      ])
      image = tf.transpose(image, [0, 1, 4, 3, 2, 5, 6, 7])
      image = tf.reshape(image, [
          -1, q_rows * m_rows * q_channnels, q_cols * m_cols * m_channels, 3
      ])
  tf.summary.image("attention", image, max_outputs=1)


def grouped_attention_single(num_groups, q, kv, q_gates, m_gates):
  """Compute grouped attention for one batch and one head.

  q is a Tensor of queries, and kv is Tensor of keys and values
  (concatenated in dimension 1).

  q_gates and m_gates are float32 Tensors containing zeros and ones.
  The ones indicate which positions belong to which groups.  A
  key-value pair can be in zero or more groups.  Each query is in one
  group.  A query can only pay attention to key-value pairs which are
  in its group.

  In addition to the usual output, we return two additional Tensors:
  q_total and m_total.

  For query position i belonging to group g, q_total[i, g] contains
  log(sum(exp(q_i dot k_j))) for all keys k_j in group g.

  For memory position j belonging to group g, m_total[j, g] contains
  the sum of the attention weights over all queries and that memory position.

  q_total and m_total contain zeros in positions where the
  corresponding query/memory does not belong to the corresponding
  group.

  Args:
    num_groups: an integer
    q: Tensor with shape [length_q, depth_qk]
    kv: Tensor with shape [length_kv, depth_qk + depth_v]
    q_gates: Tensor with shape [length_q, num_groups]
    m_gates: Tensor with shape [length_kv, num_groups]

  Returns:
    o: Tensor with shape [length_q, depth_v]
    q_total: Tensor with shape [length_q, num_groups]
    m_total: Tensor with shape [length_kv, num_groups]
  """
  q_dispatcher = expert_utils.SparseDispatcher(num_groups, q_gates)
  m_dispatcher = expert_utils.SparseDispatcher(num_groups, m_gates)
  q_length_coordinate = q_dispatcher.expert_to_batch_indices()
  m_length_coordinate = m_dispatcher.expert_to_batch_indices()
  dispatched_q = q_dispatcher.dispatch(q)
  dispatched_kv = m_dispatcher.dispatch(kv)
  length_q = tf.shape(q)[0]
  length_kv = tf.shape(kv)[0]
  depth_qk = tf.shape(q)[1]
  depth_v = tf.shape(kv)[1] - depth_qk
  o = []
  q_totals = []
  m_totals = []
  for e in xrange(num_groups):
    k, v = tf.split(dispatched_kv[e], [depth_qk, depth_v], axis=1)
    logits = tf.matmul(dispatched_q[e], k, transpose_b=True)
    log_weights = tf.nn.log_softmax(logits)
    weights = tf.exp(log_weights)
    o.append(tf.matmul(weights, v))
    # For each query, this is the log of the sum of the unnormalized weights.
    q_total = tf.reshape(logits[:, :1] - log_weights[:, :1], [-1])
    q_totals.append(tf.unsorted_segment_sum(
        q_total, q_length_coordinate[e], length_q))
    epsilon = 1e-3
    m_total = tf.log(tf.reduce_sum(tf.stop_gradient(weights), axis=0) + epsilon)
    m_totals.append(
        tf.unsorted_segment_sum(m_total, m_length_coordinate[e], length_kv))
  o = q_dispatcher.combine(o, multiply_by_gates=False)
  q_total = tf.stack(q_totals, axis=1)
  m_total = tf.stack(m_totals, axis=1)
  return o, q_total, m_total


def grouped_attention_multihead(query_antecedent,
                                memory_antecedent,
                                total_key_depth,
                                total_value_depth,
                                output_depth,
                                num_heads,
                                num_groups,
                                threshold=0.3,
                                name=None,
                                make_image_summary=True):
  """Dot-product attention with sparsity.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    num_groups: an integer
    threshold: a floating point number
    name: an optional string
    make_image_summary: a boolean

  Returns:
    A Tensor with shape [batch, length_q, output_depth]

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  batch = tf.shape(query_antecedent)[0]
  length_q = tf.shape(query_antecedent)[1]
  length_kv = tf.shape(memory_antecedent)[1]

  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  depth_qk = total_key_depth // num_heads
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  depth_v = total_value_depth // num_heads
  with tf.variable_scope(
      name,
      default_name="multihead_attention_sparse",
      values=[query_antecedent, memory_antecedent]):
    q = common_layers.conv1d(
        query_antecedent, total_key_depth, 1, name="q_transform")
    kv = common_layers.conv1d(
        memory_antecedent, total_key_depth + total_value_depth,
        1, name="kv_transform")
    q = split_heads(q, num_heads)
    kv = split_heads(kv, num_heads)
    # Make predictions about q_total and m_total.
    # These are used to determine group inclusion.
    # We will train these by auxiliary losses.  We use stop_gradient here
    # to keep these losses from back-propagating to the rest of the model.
    q_pred = common_layers.conv1d(
        tf.stop_gradient(query_antecedent), num_heads * num_groups, 1,
        name="q_pred")
    q_pred = split_heads(q_pred, num_heads)
    m_pred = common_layers.conv1d(tf.stop_gradient(
        memory_antecedent), num_heads * num_groups, 1, name="m_pred")
    m_pred = split_heads(m_pred, num_heads)
    q *= depth_qk**-0.5
    # q, kv, q_pred, m_pred are all [batch, heads, length_[q/m], ?]
    # now reshape them all to [batch * heads, length, ?]
    q = combine_first_two_dimensions(q)
    kv = combine_first_two_dimensions(kv)
    q_pred = combine_first_two_dimensions(q_pred)
    m_pred = combine_first_two_dimensions(m_pred)
    q_group = tf.argmax(q_pred, axis=2)
    q_gates = tf.one_hot(q_group, num_groups, axis=-1)
    m_gates = tf.to_float(tf.greater(m_pred, math.log(threshold)))
    # include first memory position in all groups, to avoid zero-sized tensors.
    # TODO(noam): do we need to do this for queries too?
    m_gates = tf.maximum(
        m_gates, tf.reshape(tf.one_hot([0], length_kv), [1, length_kv, 1]))
    q_group_size = tf.reduce_sum(q_gates, 1)
    m_group_size = tf.reduce_sum(m_gates, 1)

    # compute the output
    o, q_total, m_total = tf.map_fn(
        lambda args: grouped_attention_single(num_groups, *args),
        (q, kv, q_gates, m_gates),
        dtype=(tf.float32, tf.float32, tf.float32),
        parallel_iterations=1)

    # compute auxiliary losses to train the predictions
    q_loss = tf.nn.l2_loss((q_total - q_pred) * q_gates)
    q_loss /= tf.to_float(batch * length_q)
    m_loss = tf.nn.l2_loss((m_total - m_pred) * m_gates)
    m_loss /= tf.to_float(batch * length_kv)
    # We would like the query groups to be equal sized.  The group
    # size is discrete, so we need some trick here.  We add a loss
    # proportional to the product of the group size and the
    # predictions for that group.  This encourages the predictions to
    # decrease for groups that are too big.
    q_group_deviation = (q_group_size - tf.reduce_mean(
        q_group_size, axis=1, keep_dims=True)) / tf.to_float(length_kv)
    q_pred_mean = tf.reduce_mean(q_pred, axis=1)
    q_pred_mean -= tf.reduce_mean(q_pred_mean, axis=1, keep_dims=True)
    q_balance_loss = (
        tf.reduce_sum(q_pred_mean * q_group_deviation) /  tf.to_float(batch))
    extra_loss_multiplier = 1e-3
    extra_loss = (q_loss + m_loss + q_balance_loss) * extra_loss_multiplier

    # Show a bunch of summaries.
    if (not tf.get_variable_scope().reuse and
        # Summaries don't work well within tf.while_loop()
        "/while/" not in tf.contrib.framework.get_name_scope() and
        make_image_summary):
      tf.summary.histogram("q_group_size", q_group_size)
      tf.summary.histogram("m_group_size", m_group_size)
      tf.summary.scalar("q_loss", q_loss)
      tf.summary.scalar("m_loss", m_loss)
      tf.summary.scalar("q_balance_loss", q_balance_loss)
      density = (
          tf.reduce_sum(tf.to_float(m_group_size) * tf.to_float(q_group_size)) /
          tf.to_float(batch * num_heads * length_q * length_kv))
      tf.summary.scalar("density", density)
      if make_image_summary:
        # We recompute the attention for the first example, in an inefficient
        # way - masking.  This lets us show pretty pictures.
        # [num_heads, length_q, group]
        q_gates_0 = q_gates[:num_heads, :, :]
        # [num_heads, length_kv, group]
        m_gates_0 = m_gates[:num_heads, :, :]
        mask = tf.matmul(q_gates_0, m_gates_0, transpose_b=True)
        q_0 = q[:num_heads, :, :]
        k_0 = kv[:num_heads, :, :depth_qk]
        att_0 = tf.nn.softmax(tf.matmul(q_0, k_0, transpose_b=True))
        hdr = tf.pow(att_0, 0.2)  # for high-dynamic-range
        mask_channel = mask * tf.maximum(hdr, 0.3)
        image = tf.stack([hdr, mask_channel, mask_channel], axis=3)
        tf.summary.image("att", image, max_outputs=num_heads)
        mask_coverage = tf.reduce_sum(mask * att_0) / (
            tf.to_float(length_q) * num_heads)
        tf.summary.scalar("coverage", mask_coverage)

    o = tf.reshape(o, [batch, num_heads, length_q, depth_v])
    o = combine_heads(o)
    o = common_layers.conv1d(o, output_depth, 1, name="output_transform")
    return o, extra_loss


def dot_product_attention(q,
                          k,
                          v,
                          bias,
                          dropout_rate=0.0,
                          image_shapes=None,
                          name=None,
                          make_image_summary=True):
  """dot-product attention.

  Args:
    q: a Tensor with shape [batch, heads, length_q, depth_k]
    k: a Tensor with shape [batch, heads, length_kv, depth_k]
    v: a Tensor with shape [batch, heads, length_kv, depth_v]
    bias: bias Tensor (see attention_bias())
    dropout_rate: a floating point number
    image_shapes: optional tuple of integer scalars.
      see comments for attention_image_summary()
    name: an optional string
    make_image_summary: True if you want an image summary.

  Returns:
    A Tensor.
  """
  with tf.variable_scope(
      name, default_name="dot_product_attention", values=[q, k, v]):
    # [batch, num_heads, query_length, memory_length]
    logits = tf.matmul(q, k, transpose_b=True)
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    # dropping out the attention links for each of the heads
    weights = tf.nn.dropout(weights, 1.0 - dropout_rate)
    if (not tf.get_variable_scope().reuse and
        # Summaries don't work well within tf.while_loop()
        "/while/" not in tf.contrib.framework.get_name_scope() and
        make_image_summary):
      attention_image_summary(weights, image_shapes)
    return tf.matmul(weights, v)


def _generate_relative_positions_matrix(length, max_relative_position):
  """Generates matrix of relative positions between inputs."""
  range_vec = tf.range(length)
  range_mat = tf.reshape(tf.tile(range_vec, [length]), [length, length])
  distance_mat = range_mat - tf.transpose(range_mat)
  distance_mat_clipped = tf.clip_by_value(distance_mat, -max_relative_position,
                                          max_relative_position)
  # Shift values to be >= 0. Each integer still uniquely identifies a relative
  # position difference.
  final_mat = distance_mat_clipped + max_relative_position
  return final_mat


def _generate_relative_positions_embeddings(heads, length, depth,
                                            max_relative_position, name):
  """Generates tensor of size [heads, length, length, depth]."""
  with tf.variable_scope(name):
    relative_positions_matrix = _generate_relative_positions_matrix(
        length, max_relative_position)
    vocab_size = max_relative_position * 2 + 1
    # Generates embedding for each relative position of dimension heads * depth.
    embeddings_table = tf.get_variable("embeddings",
                                       [vocab_size, heads * depth])
    embeddings = tf.gather(embeddings_table, relative_positions_matrix)
    # Split embeddings per head.
    embeddings = tf.reshape(embeddings, [length, length, heads, depth])
    # Transpose to shape [heads, length, length, depth].
    embeddings = tf.transpose(embeddings, [2, 0, 1, 3])
    return embeddings


def _relative_attention_inner(x, y, z, transpose):
  """Relative position-aware dot-product attention inner calculation.

  This batches matrix multiply calculations to avoid unnecessary broadcasting.

  Args:
    x: Tensor with shape [batch_size, heads, length, length or depth].
    y: Tensor with shape [batch_size, heads, length, depth].
    z: Tensor with shape [heads, length, length, depth].
    transpose: Whether to tranpose inner matrices of y and z. Should be true if
        last dimension of x is depth, not length.

  Returns:
    A Tensor with shape [batch_size, heads, length, a].
  """
  xy_matmul = tf.matmul(x, y, transpose_b=transpose)
  x_t = tf.transpose(x, [1, 2, 0, 3])
  x_tz_matmul = tf.matmul(x_t, z, transpose_b=transpose)
  x_tz_matmul_t = tf.transpose(x_tz_matmul, [2, 0, 1, 3])
  return xy_matmul + x_tz_matmul_t


def dot_product_attention_relative(q,
                                   k,
                                   v,
                                   bias,
                                   max_relative_position,
                                   dropout_rate=0.0,
                                   image_shapes=None,
                                   name=None):
  """Calculate relative position-aware dot-product self-attention.

  The attention calculation is augmented with learned representations for the
  relative position between each element in q and each element in k and v.

  Args:
    q: a Tensor with shape [batch, heads, length, depth].
    k: a Tensor with shape [batch, heads, length, depth].
    v: a Tensor with shape [batch, heads, length, depth].
    bias: bias Tensor.
    max_relative_position: an integer specifying the maxmimum distance between
        inputs that unique position embeddings should be learned for.
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    name: an optional string.

  Returns:
    A Tensor.

  Raises:
    ValueError: if max_relative_position is not > 0.
  """
  if not max_relative_position:
    raise ValueError("Max relative position (%s) should be > 0 when using "
                     "relative self attention." % (max_relative_position))
  with tf.variable_scope(
      name, default_name="dot_product_attention_relative", values=[q, k, v]):

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape.
    q.get_shape().assert_is_compatible_with(k.get_shape())
    q.get_shape().assert_is_compatible_with(v.get_shape())

    # Use separate embeddings suitable for keys and values.
    heads = q.get_shape().as_list()[1]
    depth = q.get_shape().as_list()[3]
    length = tf.shape(q)[2]
    relations_keys = _generate_relative_positions_embeddings(
        heads, length, depth, max_relative_position, "relative_positions_keys")
    relations_values = _generate_relative_positions_embeddings(
        heads, length, depth, max_relative_position,
        "relative_positions_values")

    # Compute self attention considering the relative position embeddings.
    logits = _relative_attention_inner(q, k, relations_keys, True)
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    weights = tf.nn.dropout(weights, 1.0 - dropout_rate)
    if not tf.get_variable_scope().reuse:
      attention_image_summary(weights, image_shapes)
    return _relative_attention_inner(weights, v, relations_values, False)


def masked_local_attention_1d(
    q, k, v, block_length=128, name=None):
  """Attention to the source position and a neigborhood to the left of it.

  The sequence is divided into blocks of length block_size.
  Attention for a given query position can only see memory positions
  less than or equal to the query position, in the corresponding block
  and the previous block.

  If mask_right is True, then a target position cannot see greater source
  positions.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(name, default_name="local_attention_1d",
                         values=[q, k, v]):
    v_shape = v.get_shape()
    batch = tf.shape(q)[0]
    heads = tf.shape(q)[1]
    length = tf.shape(q)[2]
    # If (length < 2 * block_length), then we use only one block.
    block_length = tf.where(tf.less(length, block_length * 2),
                            length, block_length)
    depth_k = tf.shape(k)[3]
    depth_v = tf.shape(v)[3]
    original_length = length
    padding_size = tf.mod(-length, block_length)
    length += padding_size
    padding = [[0, 0], [0, 0], [0, padding_size], [0, 0]]
    q = tf.pad(q, padding)
    k = tf.pad(k, padding)
    v = tf.pad(v, padding)
    num_blocks = tf.div(length, block_length)

    # compute attention for the first query block.
    first_q = tf.slice(q, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_k = tf.slice(k, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_v = tf.slice(v, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_output = dot_product_attention(
        first_q, first_k, first_v, attention_bias_lower_triangle(block_length),
        name="fist_block")

    # compute attention for all subsequent query blocks.
    q = tf.reshape(q, [batch, heads, num_blocks, block_length, depth_k])
    k = tf.reshape(k, [batch, heads, num_blocks, block_length, depth_k])
    v = tf.reshape(v, [batch, heads, num_blocks, block_length, depth_v])

    def local(x):
      """Create a local version of the keys or values."""
      prev_block = tf.slice(
          x, [0, 0, 0, 0, 0], [-1, -1, num_blocks - 1, -1, -1])
      cur_block = tf.slice(
          x, [0, 0, 1, 0, 0], [-1, -1, -1, -1, -1])
      return tf.concat([prev_block, cur_block], 3)
    local_k = local(k)
    local_v = local(v)
    tail_q = tf.slice(q, [0, 0, 1, 0, 0], [-1, -1, -1, -1, -1])

    local_length = tf.shape(local_k)[3]

    # [batch, heads, num_blocks - 1, block_length, local_length]
    attention = tf.matmul(tail_q, local_k, transpose_b=True)

    # make sure source_pos <= target_pos
    good_part = tf.matrix_band_part(
        tf.ones([block_length, local_length]), -1, tf.to_int64(block_length))
    mask = (1.0 - good_part) * -1e9
    attention += tf.reshape(mask, [1, 1, 1, block_length, local_length])
    attention = tf.nn.softmax(attention)
    # TODO(noam): figure out how to show a summary for the remaining blocks.
    # The naive way currently causes errors due to empty tensors.
    # output: [batch, heads, num_blocks-1, block_length, depth_v]
    output = tf.matmul(attention, local_v)
    output = tf.reshape(output, [batch, heads, -1, depth_v])
    output = tf.concat([first_output, output], axis=2)
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_shape)
    return output


def local_attention_1d(q,
                       k,
                       v,
                       block_length=128,
                       filter_width=100,
                       name=None):
  """strided block local self-attention.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    filter_width: an integer indicating how much to look left.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_self_attention_1d", values=[q, k, v]):
    v_shape = v.get_shape()
    depth_v = tf.shape(v)[3]
    batch_size = tf.shape(q)[0]
    num_heads = tf.shape(q)[1]
    original_length = tf.shape(q)[2]

    # making sure q is a multiple of d
    def pad_to_multiple(x, pad_length):
      x_length = tf.shape(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l_and_r(x, pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [pad_length, pad_length], [0, 0]])

    q = pad_to_multiple(q, block_length)
    k = pad_to_multiple(k, block_length)
    v = pad_to_multiple(v, block_length)

    # Setting up q blocks
    new_q_shape = tf.shape(q)
    # Setting up q blocks
    q = tf.reshape(q, [
        new_q_shape[0], new_q_shape[1], new_q_shape[2] // block_length,
        block_length, new_q_shape[3]
    ])

    # Setting up k and v values
    k = pad_l_and_r(k, filter_width)
    v = pad_l_and_r(v, filter_width)

    length = tf.shape(k)[2]
    full_filter_width = block_length + 2 * filter_width
    # getting gather indices
    indices = tf.range(0, length, delta=1, name="index_range")
    # making indices [1, length, 1] to appy convs
    indices = tf.reshape(indices, [1, -1, 1])
    kernel = tf.expand_dims(tf.eye(full_filter_width), axis=1)
    gather_indices = tf.nn.conv1d(
        tf.cast(indices, tf.float32),
        kernel,
        block_length,
        padding="VALID",
        name="gather_conv")

    gather_indices = tf.squeeze(tf.cast(gather_indices, tf.int32), axis=0)

    # [length, batch, heads, dim]
    k_t = tf.transpose(k, [2, 0, 1, 3])
    k_new = tf.gather(k_t, gather_indices)

    # [batch, heads, blocks, block_length, dim]
    k_new = tf.transpose(k_new, [2, 3, 0, 1, 4])

    attention_bias = tf.expand_dims(embedding_to_padding(k_new) * -1e9, axis=-2)

    v_t = tf.transpose(v, [2, 0, 1, 3])
    v_new = tf.gather(v_t, gather_indices)
    v_new = tf.transpose(v_new, [2, 3, 0, 1, 4])

    output = dot_product_attention(
        q, k_new, v_new, attention_bias, dropout_rate=0., name="local_1d",
        make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_shape)
    return output


def reshape_by_blocks(x, x_shape, memory_block_size):
  x = tf.reshape(x, [
      x_shape[0], x_shape[1], x_shape[2] // memory_block_size,
      memory_block_size, x_shape[3]
  ])
  return x


def dilated_self_attention_1d(q,
                              k,
                              v,
                              query_block_size=128,
                              memory_block_size=128,
                              gap_size=2,
                              num_memory_blocks=2,
                              name=None):
  """dilated self-attention.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    query_block_size: an integer indicating size of query block
    memory_block_size: an integer indicating the size of a memory block.
    gap_size: an integer indicating the gap size
    num_memory_blocks: how many memory blocks to look at to the left and right.
      Each will be separated by gap_size.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="dilated_self_attention_1d", values=[q, k, v]):
    v_list_shape = v.get_shape().as_list()
    v_shape = tf.shape(v)
    depth_v = v_shape[3]
    batch_size = v_shape[0]
    num_heads = v_shape[1]
    original_length = tf.shape(q)[2]
    # making sure q is a multiple of query block size
    def pad_to_multiple(x, pad_length):
      x_length = tf.shape(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l_and_r(x, pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [pad_length, pad_length], [0, 0]])

    q = pad_to_multiple(q, query_block_size)
    v = pad_to_multiple(v, query_block_size)
    k = pad_to_multiple(k, query_block_size)

    q.set_shape(v_list_shape)
    v.set_shape(v_list_shape)
    k.set_shape(v_list_shape)
    # Setting up q blocks
    new_q_shape = tf.shape(q)
    # Setting up q blocks
    q = reshape_by_blocks(q, new_q_shape, query_block_size)
    self_k_part = reshape_by_blocks(k, new_q_shape, query_block_size)
    self_v_part = reshape_by_blocks(v, new_q_shape, query_block_size)

    # Setting up k and v windows
    k_v_padding = (gap_size + memory_block_size) * num_memory_blocks
    k = pad_l_and_r(k, k_v_padding)
    v = pad_l_and_r(v, k_v_padding)
    # getting gather indices
    index_length = (new_q_shape[2] - query_block_size + memory_block_size)
    indices = tf.range(0, index_length, delta=1, name="index_range")
    # making indices [1, length, 1] to appy convs
    indices = tf.reshape(indices, [1, -1, 1])
    kernel = tf.expand_dims(tf.eye(memory_block_size), axis=1)
    gather_indices = tf.nn.conv1d(
        tf.cast(indices, tf.float32),
        kernel,
        query_block_size,
        padding="VALID",
        name="gather_conv")

    gather_indices = tf.squeeze(tf.cast(gather_indices, tf.int32), axis=0)

    # get left and right memory blocks for each query
    # [length, batch, heads, dim]
    k_t = tf.transpose(k, [2, 0, 1, 3])
    v_t = tf.transpose(v, [2, 0, 1, 3])
    left_k = gather_dilated_memory_blocks(k_t[:-k_v_padding, :, :, :],
                                          num_memory_blocks, gap_size,
                                          query_block_size, memory_block_size,
                                          gather_indices)
    left_v = gather_dilated_memory_blocks(v_t[:-k_v_padding, :, :, :],
                                          num_memory_blocks, gap_size,
                                          query_block_size, memory_block_size,
                                          gather_indices)

    right_k = gather_dilated_memory_blocks(k_t[k_v_padding:, :, :, :],
                                           num_memory_blocks, gap_size,
                                           query_block_size, memory_block_size,
                                           gather_indices, direction="right")
    right_v = gather_dilated_memory_blocks(v_t[k_v_padding:, :, :, :],
                                           num_memory_blocks, gap_size,
                                           query_block_size, memory_block_size,
                                           gather_indices, direction="right")

    k_windows = tf.concat([left_k, self_k_part, right_k], axis=3)
    v_windows = tf.concat([left_v, self_v_part, right_v], axis=3)
    attention_bias = tf.expand_dims(
        embedding_to_padding(k_windows) * -1e9, axis=-2)

    output = dot_product_attention(
        q, k_windows, v_windows, attention_bias, dropout_rate=0.,
        name="dilated_1d", make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_list_shape)
    return output


def gather_dilated_memory_blocks(x, num_memory_blocks, gap_size,
                                 query_block_size, memory_block_size,
                                 gather_indices, direction="left"):
  """Gathers blocks with gaps in between.

  Args:
    x: A tensor of shape [length, batch, heads, depth]
    num_memory_blocks:     num_memory_blocks: how many memory blocks to look
      in "direction". Each will be separated by gap_size.
    gap_size: an integer indicating the gap size
    query_block_size: an integer indicating size of query block
    memory_block_size: an integer indicating the size of a memory block.
    gather_indices: The indices to gather from.
    direction: left or right
  Returns:
    a tensor of shape [batch, heads, blocks, block_length, depth]
  """

  gathered_blocks = []
  # gathering memory blocks
  for block_id in range(num_memory_blocks):
    block_end_index = -(query_block_size +
                        gap_size * (block_id+1) + memory_block_size *
                        block_id) - 1
    block_start_index = (
        (memory_block_size + gap_size) *
        (num_memory_blocks - (block_id + 1))
    )
    if direction != "left":
      [block_end_index, block_start_index] = [
          -block_start_index - 1, -block_end_index + 1
      ]
    def gather_dilated_1d_blocks(x, gather_indices):
      x_new = tf.gather(x, gather_indices)
      # [batch, heads, blocks, block_length, dim]
      return tf.transpose(x_new, [2, 3, 0, 1, 4])

    gathered_blocks.append(
        gather_dilated_1d_blocks(x[block_start_index:block_end_index],
                                 gather_indices))
  return tf.concat(gathered_blocks, 3)


def masked_dilated_self_attention_1d(q,
                                     k,
                                     v,
                                     query_block_size=64,
                                     memory_block_size=64,
                                     gap_size=2,
                                     num_memory_blocks=2,
                                     name=None):
  """dilated self-attention.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    query_block_size: an integer
    memory_block_size: an integer indicating how much to look left.
    gap_size: an integer indicating the gap size
    num_memory_blocks: how many memory blocks to look at to the left. Each will
      be separated by gap_size.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="masked_dilated_self_attention_1d", values=[q, k, v]):
    v_list_shape = v.get_shape().as_list()
    v_shape = tf.shape(v)
    depth_v = v_shape[3]
    batch_size = v_shape[0]
    num_heads = v_shape[1]
    original_length = tf.shape(q)[2]
    # making sure q is a multiple of query block size
    def pad_to_multiple(x, pad_length):
      x_length = tf.shape(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l(x, left_pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [left_pad_length, 0], [0, 0]])

    q = pad_to_multiple(q, query_block_size)
    v = pad_to_multiple(v, query_block_size)
    k = pad_to_multiple(k, query_block_size)
    q.set_shape(v_list_shape)
    v.set_shape(v_list_shape)
    k.set_shape(v_list_shape)
    # Setting up q blocks
    new_q_shape = tf.shape(q)

    # Setting up q blocks
    q = reshape_by_blocks(q, new_q_shape, query_block_size)
    self_k_part = reshape_by_blocks(k, new_q_shape, query_block_size)
    self_v_part = reshape_by_blocks(v, new_q_shape, query_block_size)
    # Setting up k and v windows
    k_v_padding = (gap_size + memory_block_size) * num_memory_blocks
    k = pad_l(k, k_v_padding)
    v = pad_l(v, k_v_padding)
    # getting gather indices
    index_length = (new_q_shape[2] - query_block_size + memory_block_size)

    indices = tf.range(0, index_length, delta=1, name="index_range")
    # making indices [1, length, 1] to appy convs
    indices = tf.reshape(indices, [1, -1, 1])
    kernel = tf.expand_dims(tf.eye(memory_block_size), axis=1)
    gather_indices = tf.nn.conv1d(
        tf.cast(indices, tf.float32),
        kernel,
        query_block_size,
        padding="VALID",
        name="gather_conv")
    gather_indices = tf.squeeze(tf.cast(gather_indices, tf.int32), axis=0)

    # get left and right memory blocks for each query
    # [length, batch, heads, dim]
    k_t = tf.transpose(k, [2, 0, 1, 3])
    v_t = tf.transpose(v, [2, 0, 1, 3])

    k_unmasked_windows = gather_dilated_memory_blocks(k_t, num_memory_blocks,
                                                      gap_size,
                                                      query_block_size,
                                                      memory_block_size,
                                                      gather_indices)
    v_unmasked_windows = gather_dilated_memory_blocks(v_t, num_memory_blocks,
                                                      gap_size,
                                                      query_block_size,
                                                      memory_block_size,
                                                      gather_indices)

    # combine memory windows
    block_q_shape = tf.shape(q)
    masked_attention_bias = tf.tile(tf.expand_dims(
        attention_bias_lower_triangle(query_block_size), axis=0),
                                    [block_q_shape[0], block_q_shape[1],
                                     block_q_shape[2], 1, 1])
    padding_attention_bias = tf.expand_dims(
        embedding_to_padding(k_unmasked_windows) * -1e9, axis=-2)
    padding_attention_bias = tf.tile(padding_attention_bias,
                                     [1, 1, 1, query_block_size, 1])
    attention_bias = tf.concat([masked_attention_bias, padding_attention_bias],
                               axis=-1)
    # combine memory windows
    k_windows = tf.concat([self_k_part, k_unmasked_windows], 3)
    v_windows = tf.concat([self_v_part, v_unmasked_windows], 3)
    output = dot_product_attention(
        q, k_windows, v_windows, attention_bias, dropout_rate=0.,
        name="dilated_1d", make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_list_shape)
    return output


def local_attention_2d(q,
                       k,
                       v,
                       query_shape=(8, 16),
                       memory_flange=(8, 16),
                       name=None):
  """strided block local self-attention.

  Args:
    q: a Tensor with shape [batch, heads, h, w, depth_k]
    k: a Tensor with shape [batch, heads, h, w, depth_k]
    v: a Tensor with shape [batch, heads, h, w, depth_v]
    query_shape: an tuple indicating the height and width of each query block.
    memory_flange: an integer indicating how much to look in height and width
      from each query block.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, h, w, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_self_attention_2d", values=[q, k, v]):
    q_shape = q.get_shape().as_list()
    v_shape = tf.shape(v)

    q = pad_to_multiple_2d(q, query_shape)
    k = pad_to_multiple_2d(k, query_shape)
    v = pad_to_multiple_2d(v, query_shape)
    padded_q_shape = tf.shape(q)
    # Setting up k and v values
    paddings = [[0, 0], [0, 0], [memory_flange[0], memory_flange[1]],
                [memory_flange[0], memory_flange[1]], [0, 0]]
    k = tf.pad(k, paddings)
    v = tf.pad(v, paddings)

    # Setting up q blocks
    q_indices = gather_indices_2d(q, query_shape, query_shape)
    q_new = gather_blocks_2d(q, q_indices)

    # Setting up k and v blocks
    memory_shape = (query_shape[0]+2*memory_flange[0],
                    query_shape[1]+2*memory_flange[1])
    k_and_v_indices = gather_indices_2d(k, memory_shape, query_shape)
    k_new = gather_blocks_2d(k, k_and_v_indices)
    v_new = gather_blocks_2d(v, k_and_v_indices)

    attention_bias = tf.expand_dims(
        tf.to_float(embedding_to_padding(k_new)) * -1e9, axis=-2)

    output = dot_product_attention(q_new, k_new, v_new, attention_bias,
                                   dropout_rate=0., name="local_2d",
                                   make_image_summary=False)
    # putting the representations back in the right place
    output = scatter_blocks_2d(output, q_indices, padded_q_shape)
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0, 0],
                      [-1, -1, v_shape[2], v_shape[3], -1])
    output.set_shape(q_shape)
    return output


def pad_to_multiple_2d(x, block_shape):
  """Making sure x is a multiple of shape."""
  old_shape = x.get_shape().dims
  last = old_shape[-1]
  height_padding = -tf.shape(x)[2] % block_shape[0]
  width_padding = -tf.shape(x)[3] % block_shape[1]
  paddings = [[0, 0], [0, 0], [0, height_padding],
              [0, width_padding], [0, 0]]
  padded_x = tf.pad(x, paddings)
  padded_shape = padded_x.get_shape().as_list()
  padded_shape = padded_shape[:-1]+[last]
  padded_x.set_shape(padded_shape)
  return padded_x


def reshape_range(tensor, i, j, shape):
  """Reshapes a tensor between dimensions i and j."""
  target_shape = tf.concat(
      [tf.shape(tensor)[:i], shape, tf.shape(tensor)[j:]],
      axis=0)
  return tf.reshape(tensor, target_shape)


def gather_blocks_2d(x, indices):
  """Gathers flattened blocks from x."""
  x_shape = tf.shape(x)
  x = reshape_range(x, 2, 4, [tf.reduce_prod(x_shape[2:4])])
  # [length, batch, heads, dim]
  x_t = tf.transpose(x, [2, 0, 1, 3])
  x_new = tf.gather(x_t, indices)
  # returns [batch, heads, num_blocks, block_length ** 2, dim]
  return tf.transpose(x_new, [2, 3, 0, 1, 4])


def scatter_blocks_2d(x, indices, shape):
  """scatters blocks from x into shape with indices."""
  x_shape = tf.shape(x)
  # [length, batch, heads, dim]
  x_t = tf.transpose(tf.reshape(x, [x_shape[0], x_shape[1], -1, x_shape[-1]]),
                     [2, 0, 1, 3])
  x_t_shape = tf.shape(x_t)
  indices = tf.reshape(indices, [-1, 1])
  scattered_x = tf.scatter_nd(indices, x_t, x_t_shape)
  scattered_x = tf.transpose(scattered_x, [1, 2, 0, 3])
  return tf.reshape(scattered_x, shape)


def gather_indices_2d(x, block_shape, block_stride):
  """Getting gather indices."""
  # making an identity matrix kernel
  kernel = tf.eye(block_shape[0]*block_shape[1])
  kernel = reshape_range(kernel, 0, 1, [block_shape[0], block_shape[1], 1])
  # making indices [1, h, w, 1] to appy convs
  indices = tf.range(0, tf.shape(x)[2] * tf.shape(x)[3], delta=1)
  indices = tf.reshape(indices, [1, tf.shape(x)[2], tf.shape(x)[3], 1])
  indices = tf.nn.conv2d(
      tf.cast(indices, tf.float32),
      kernel,
      strides=[1, block_stride[0], block_stride[1], 1],
      padding="VALID")
  # making indices [num_blocks, dim] to gather
  num_blocks = tf.reduce_prod(tf.shape(indices)[:3])
  indices = tf.reshape(indices, [num_blocks, -1])
  return tf.cast(indices, tf.int32)


def make_2d_block_raster_mask(query_shape, memory_flange):
  """creates a mask for 2d block raster scany.

  The query mask can look to the left, top left, top, and top right, but
  not to the right. Inside the query, we have the standard raster scan
  masking.
  Args:
    query_shape: A tuple of ints (query_height, query_width)
    memory_flange: A tuple of ints
      (memory_flange_height, memory_flange_width)

  Returns:
    A tensor of shape query_size, memory_size
  """
  # mask inside the query block
  query_triangle = tf.matrix_band_part(
      tf.ones([np.prod(query_shape), np.prod(query_shape)]), -1, 0)
  split_query_masks = tf.split(query_triangle, query_shape[0], axis=1)
  # adding mask for left and right
  mask_pieces = [
      tf.concat(
          [tf.ones([np.prod(query_shape), memory_flange[1]]),
           split_query_masks[i],
           tf.zeros([np.prod(query_shape), memory_flange[1]])
          ], axis=1) for i in range(query_shape[0])]
  # adding mask for top
  final_mask = tf.concat(
      [tf.ones(
          [np.prod(query_shape),
           (query_shape[1]+2*memory_flange[1])*memory_flange[0]]),
       tf.concat(mask_pieces, axis=1)
      ], axis=1)
  # 0. is visible location, 1.0 is masked.
  return 1. - final_mask


def masked_local_attention_2d(q,
                              k,
                              v,
                              query_shape=(8, 16),
                              memory_flange=(8, 16),
                              name=None):
  """strided block local self-attention.

  Args:
    q: a Tensor with shape [batch, heads, h, w, depth_k]
    k: a Tensor with shape [batch, heads, h, w, depth_k]
    v: a Tensor with shape [batch, heads, h, w, depth_v]
    query_shape: an tuple indicating the height and width of each query block.
      query_shape = block_shape
    memory_flange: an integer indicating how much to look in height and width
      from each query block.
      memory shape = query_shape + (block_flange[0], 2*block_flange[1])
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, h, w, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_masked_self_attention_2d", values=[q, k, v]):
    q_shape = q.get_shape().as_list()
    v_shape = tf.shape(v)

    q = pad_to_multiple_2d(q, query_shape)
    padded_q_shape = tf.shape(q)
    k = pad_to_multiple_2d(k, query_shape)
    v = pad_to_multiple_2d(v, query_shape)
    # Setting up k and v values. Padding top, left, and right
    paddings = [[0, 0], [0, 0], [memory_flange[0], 0],
                [memory_flange[1], memory_flange[1]], [0, 0]]
    k = tf.pad(k, paddings)
    v = tf.pad(v, paddings)
    # Setting up q blocks
    q_indices = gather_indices_2d(q, query_shape, query_shape)
    q_new = gather_blocks_2d(q, q_indices)
    # Setting up k and v blocks
    memory_shape = (query_shape[0]+memory_flange[0],
                    query_shape[1]+memory_flange[1]*2)
    k_and_v_indices = gather_indices_2d(k, memory_shape, query_shape)
    k_new = gather_blocks_2d(k, k_and_v_indices)
    v_new = gather_blocks_2d(v, k_and_v_indices)
    # Combining the mask for padding and visible region
    attention_mask_shape = [np.prod(query_shape),
                            (query_shape[0]+memory_flange[0])*
                            (query_shape[1]+2*memory_flange[1])]
    attention_mask = tf.cast(
        make_2d_block_raster_mask(query_shape, memory_flange), tf.bool)
    # reshaping attention mask to have same dims as logits
    attention_mask = tf.reshape(attention_mask, [1, 1, 1]+attention_mask_shape)
    padding_mask = tf.expand_dims(
        tf.cast(embedding_to_padding(k_new), tf.bool), axis=-2)
    attention_bias = (
        tf.to_float(tf.logical_or(attention_mask, padding_mask)) *-1e9)
    output = dot_product_attention(q_new, k_new, v_new, attention_bias,
                                   dropout_rate=0., name="masked_local_2d",
                                   make_image_summary=False)
    # putting the representations back in the right place
    output = scatter_blocks_2d(output, q_indices, padded_q_shape)
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0, 0],
                      [-1, -1, v_shape[2], v_shape[3], -1])
    output.set_shape(q_shape)
    return output


def compute_qkv(query_antecedent, memory_antecedent, total_key_depth,
                total_value_depth, q_filter_width=1, kv_filter_width=1,
                q_padding="VALID", kv_padding="VALID"):
  """Computes query, key and value.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels]
    total_key_depth: an integer
    total_value_depth: and integer
    q_filter_width: An integer specifying how wide you want the query to be.
    kv_filter_width: An integer specifying how wide you want the keys and values
    to be.
    q_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    kv_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.

  Returns:
    q, k, v : [batch, length, depth] tensors
  """
  if memory_antecedent is None and q_filter_width == kv_filter_width == 1:
    # self attention with single position q, k, and v
    combined = common_layers.conv1d(
        query_antecedent,
        total_key_depth * 2 + total_value_depth,
        1,
        name="qkv_transform")
    q, k, v = tf.split(
        combined, [total_key_depth, total_key_depth, total_value_depth],
        axis=2)
    return q, k, v

  if memory_antecedent is None:
    # self attention
    q = common_layers.conv1d(
        query_antecedent,
        total_key_depth,
        q_filter_width,
        padding=q_padding,
        name="q_transform")
    kv_combined = common_layers.conv1d(
        query_antecedent,
        total_key_depth + total_value_depth,
        kv_filter_width,
        padding=kv_padding,
        name="kv_transform")
    k, v = tf.split(kv_combined, [total_key_depth, total_value_depth],
                    axis=2)
    return q, k, v

  # encoder-decoder attention
  q = common_layers.conv1d(
      query_antecedent, total_key_depth, q_filter_width, padding=q_padding,
      name="q_transform")
  combined = common_layers.conv1d(
      memory_antecedent,
      total_key_depth + total_value_depth,
      1,
      padding=kv_padding,
      name="kv_transform")
  k, v = tf.split(combined, [total_key_depth, total_value_depth], axis=2)

  return q, k, v


def compute_qkv_2d(query_antecedent, memory_antecedent, total_key_depth,
                   total_value_depth):
  """Computes query, key and value.

  Args:
    query_antecedent: a Tensor with shape [batch, h, w, depth_k]
    memory_antecedent: a Tensor with shape [batch, h, w, depth_k]
    total_key_depth: an integer
    total_value_depth: and integer

  Returns:
    q, k, v : [batch, h, w, depth_k] tensors
  """
  # self attention with single position q, k, and v
  if memory_antecedent is None:
    combined = tf.layers.conv2d(
        query_antecedent,
        total_key_depth * 2 + total_value_depth, (1, 1),
        name="qkv_transform")
    q, k, v = tf.split(
        combined, [total_key_depth, total_key_depth, total_value_depth],
        axis=-1)
    return q, k, v

  # Encoder decoder attention
  q = common_layers.conv1d(
      query_antecedent, total_key_depth, 1, name="q_transform")
  combined = common_layers.conv1d(
      memory_antecedent,
      total_key_depth + total_value_depth,
      1,
      name="kv_transform")
  k, v = tf.split(combined, [total_key_depth, total_value_depth], axis=2)

  return q, k, v


def multihead_attention(query_antecedent,
                        memory_antecedent,
                        bias,
                        total_key_depth,
                        total_value_depth,
                        output_depth,
                        num_heads,
                        dropout_rate,
                        max_relative_position=None,
                        image_shapes=None,
                        attention_type="dot_product",
                        block_length=128,
                        block_width=128,
                        q_filter_width=1,
                        kv_filter_width=1,
                        q_padding="VALID",
                        kv_padding="VALID",
                        cache=None,
                        gap_size=0,
                        num_memory_blocks=2,
                        name=None,
                        **kwargs):
  """Multihead scaled-dot-product attention with input/output transformations.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels]
    bias: bias Tensor (see attention_bias())
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    dropout_rate: a floating point number
    max_relative_position: Maximum distance between inputs to generate
                           unique relation embeddings for. Only relevant
                           when using dot_product_relative attention.
    image_shapes: optional tuple of integer scalars.
      see comments for attention_image_summary()
    attention_type: a string, either "dot_product", "local_mask_right",
                    "local_unmasked" or any attention function with the
                    signature (q, k, v, **kwargs)
    block_length: an integer - relevant for "local_mask_right"
    block_width: an integer - relevant for "local_unmasked"
    q_filter_width: An integer specifying how wide you want the query to be.
    kv_filter_width: An integer specifying how wide you want the keys and values
    to be.
    q_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    kv_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    cache: dict, containing Tensors which are the results of previous
        attentions, used for fast decoding. Expects the dict to contrain two
        keys; 'k' and 'v', for the initial call the values for these keys should
        be empty Tensors of the appropriate shape.
            'k' [batch_size, 0, key_channels]
            'v' [batch_size, 0, value_channels]
    gap_size: Integer option for dilated attention to indicate spacing between
      memory blocks.
    num_memory_blocks: Integer option to indicate how many memory blocks to look
      at.
    name: an optional string
    **kwargs (dict): Params for the attention function

  Caching:
    WARNING: For decoder self-attention, i.e. when memory_antecedent == None,
    the caching assumes that the bias contains future masking.

    The caching works by saving all the previous key and value values so that
    you are able to send just the last query location to this attention
    function. I.e. if the cache dict is provided it assumes the query is of the
    shape [batch_size, 1, hiddem_dim] rather than the full memory.

  Returns:
    The result of the attention transformation. The output shape is
        [batch_size, length_q, hidden_dim]
    unless the cache dict is provided in which case only the last memory
    position is calculated and the output shape is [batch_size, 1, hidden_dim]
    Optionnaly return an additional loss parameters (ex: load balance loss for
    the experts) returned by the attention_type function

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  with tf.variable_scope(
      name,
      default_name="multihead_attention",
      values=[query_antecedent, memory_antecedent]):
    q, k, v = compute_qkv(query_antecedent, memory_antecedent, total_key_depth,
                          total_value_depth, q_filter_width, kv_filter_width,
                          q_padding, kv_padding)

    if cache is not None:
      if attention_type != "dot_product":
        raise NotImplementedError(
            "Caching is not guaranteed to work with attention types other than"
            " dot_product.")
      if bias is None:
        raise ValueError("Bias required for caching. See function docstring "
                         "for details.")
      k = cache["k"] = tf.concat([cache["k"], k], axis=1)
      v = cache["v"] = tf.concat([cache["v"], v], axis=1)

    q = split_heads(q, num_heads)
    k = split_heads(k, num_heads)
    v = split_heads(v, num_heads)
    key_depth_per_head = total_key_depth // num_heads
    q *= key_depth_per_head**-0.5

    additional_returned_value = None
    if callable(attention_type):  # Generic way to extend multihead_attention
      x = attention_type(q, k, v, **kwargs)
      if isinstance(x, tuple):
        x, additional_returned_value = x  # Unpack
    elif attention_type == "dot_product":
      x = dot_product_attention(q, k, v, bias, dropout_rate, image_shapes)
    elif attention_type == "dot_product_relative":
      x = dot_product_attention_relative(q, k, v, bias, max_relative_position,
                                         dropout_rate, image_shapes)
    elif attention_type == "local_mask_right":
      x = masked_local_attention_1d(q, k, v, block_length=block_length)
    elif attention_type == "local_unmasked":
      x = local_attention_1d(
          q, k, v, block_length=block_length, filter_width=block_width)
    elif attention_type == "masked_dilated_1d":
      x = masked_dilated_self_attention_1d(q, k, v, block_length,
                                           block_width,
                                           gap_size,
                                           num_memory_blocks)
    else:
      assert attention_type == "unmasked_dilated_1d"
      x = dilated_self_attention_1d(q, k, v, block_length,
                                    block_width,
                                    gap_size,
                                    num_memory_blocks)
    x = combine_heads(x)
    x = common_layers.conv1d(x, output_depth, 1, name="output_transform")
    if additional_returned_value is not None:
      return x, additional_returned_value
    return x


def multihead_attention_2d(query_antecedent,
                           memory_antecedent,
                           total_key_depth,
                           total_value_depth,
                           output_depth,
                           num_heads,
                           attention_type="local_attention_2d",
                           query_shape=(8, 16),
                           memory_flange=(8, 16),
                           name=None):
  """2d Multihead scaled-dot-product attention with inp/output transformations.

  Args:
    query_antecedent: a Tensor with shape [batch, h, w, depth_k]
    memory_antecedent: a Tensor with shape [batch, h, w, depth_k]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    attention_type: String, type of attention function to use.
    query_shape: an tuple indicating the height and width of each query block.
    memory_flange: an integer indicating how much to look in height and width
    name: an optional string

  Returns:
    A Tensor of shape [batch, h, w, depth_k]

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  with tf.variable_scope(
      name,
      default_name="multihead_attention_2d",
      values=[query_antecedent, memory_antecedent]):
    q, k, v = compute_qkv_2d(query_antecedent, memory_antecedent,
                             total_key_depth, total_value_depth)
    # after splitting, shape is [batch, heads, h, w, depth]
    q = split_heads_2d(q, num_heads)
    k = split_heads_2d(k, num_heads)
    v = split_heads_2d(v, num_heads)
    key_depth_per_head = total_key_depth // num_heads
    q *= key_depth_per_head**-0.5
    if attention_type == "local_attention_2d":
      x = local_attention_2d(
          q, k, v, query_shape=query_shape, memory_flange=memory_flange)
    else:
      assert attention_type == "masked_local_attention_2d"
      x = masked_local_attention_2d(q, k, v, query_shape=query_shape,
                                    memory_flange=memory_flange)
    x = combine_heads_2d(x)
    x = tf.layers.conv2d(
        x,
        output_depth,
        (1, 1),
        name="output_transform")
    return x


def ffn_self_attention_layer(x,
                             filter_depth,
                             output_depth,
                             num_parts,
                             dropout_rate,
                             share_kv=False,
                             name=None):
  """Self-attention feedforward layer.

  We use self-attention to do feedforward computations. We apply this function
  positionwise where for each position, we linearly transform the output to have
  depth filter_depth, and break up the result depth-wise into num_parts
  contiguous parts.  The parts self-attentd, we concatenate the results
  depth-wise, and we linearly transform to a depth of output_depth. The
  goal is to get multiplicative interactions between components of a
  representation.

  Args:
    x: a Tensor with shape [batch, length, channels]
    filter_depth: an integer
    output_depth: an integer
    num_parts: an integer dividing filter depth
    dropout_rate: a floating point number
    share_kv: Share the key value transform
    name: an optional string

  Returns:
    A Tensor.
  """

  with tf.variable_scope(
      name, default_name="feedforward_self_attention", values=[x]):
    x_shape = tf.shape(x)
    part_depth = filter_depth // num_parts
    if not share_kv:
      combined = common_layers.conv1d(
          x, filter_depth * 3, 1, name="qkv_transform")
      combined = tf.expand_dims(combined, axis=2)
      q, k, v = tf.split(combined, 3, axis=3)
    else:
      q = tf.expand_dims(
          common_layers.conv1d(x, filter_depth, 1, name="q_transform"), axis=2)
      kv_combined = tf.expand_dims(
          common_layers.conv1d(
              tf.concat([x, x], axis=1), filter_depth, 1, name="kv_transform"),
          axis=2)
      k, v = tf.split(kv_combined, [x_shape[1], x_shape[1]], axis=1)

    batch_q = tf.reshape(q, [-1, 1, num_parts, part_depth])
    batch_k = tf.reshape(k, [-1, 1, num_parts, part_depth])
    batch_v = tf.reshape(v, [-1, 1, num_parts, part_depth])

    batch_q *= part_depth**-0.5
    # non-masked bias
    bias = None
    x = dot_product_attention(batch_q, batch_k, batch_v, bias, dropout_rate)
    x = tf.reshape(x, [x_shape[0], x_shape[1], filter_depth])
    x = common_layers.conv1d(x, output_depth, 1, name="output_transform")
    return x


def parameter_attention(x,
                        total_key_depth,
                        total_value_depth,
                        output_depth,
                        memory_rows,
                        num_heads,
                        dropout_rate,
                        name=None):
  """Attention over parameters.

  We use the same multi-headed attention as in the other layers, but the memory
  keys and values are model parameters.  There are no linear transformation
  on the keys or values.

  We are also a bit more careful about memory usage, since the number of
  memory positions may be very large.

  Args:
    x: a Tensor with shape [batch, length_q, channels]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    memory_rows: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    dropout_rate: a floating point number
    name: an optional string

  Returns:
    A Tensor.
  """
  with tf.variable_scope(name, default_name="parameter_attention", values=[x]):
    head_size_k = total_key_depth // num_heads
    head_size_v = total_value_depth // num_heads
    var_shape_k = [num_heads, memory_rows, head_size_k]
    var_shape_v = [num_heads, memory_rows, head_size_v]
    k = tf.get_variable(
        "k",
        var_shape_k,
        initializer=tf.random_normal_initializer(0, output_depth**-0.5)) * (
            num_heads**0.5)
    v = tf.get_variable(
        "v",
        var_shape_v,
        initializer=tf.random_normal_initializer(0, output_depth**-0.5)) * (
            output_depth**0.5)
    batch_size = tf.shape(x)[0]
    length = tf.shape(x)[1]
    q = common_layers.conv1d(x, total_key_depth, 1, name="q_transform")
    if dropout_rate:
      # This is a cheaper form of attention dropout where we use to use
      # the same dropout decisions across batch elemets and query positions,
      # but different decisions across heads and memory positions.
      v = tf.nn.dropout(
          v, 1.0 - dropout_rate, noise_shape=[num_heads, memory_rows, 1])
    # query is [batch, length, hidden_size]
    # reshape and transpose it to [heads, batch * length, head_size]
    q = tf.reshape(q, [batch_size, length, num_heads, head_size_k])
    q = tf.transpose(q, [2, 0, 1, 3])
    q = tf.reshape(q, [num_heads, batch_size * length, head_size_k])
    weights = tf.matmul(q, k, transpose_b=True)
    weights = tf.nn.softmax(weights)
    y = tf.matmul(weights, v)
    y = tf.reshape(y, [num_heads, batch_size, length, head_size_v])
    y = tf.transpose(y, [1, 2, 0, 3])
    y = tf.reshape(y, [batch_size, length, total_value_depth])
    y.set_shape([None, None, total_value_depth])
    y = common_layers.conv1d(y, output_depth, 1, name="output_transform")
    return y


def coordinate_tensor(shape, axis):
  """Return a tensor with given shape containing coordinte along given axis.

  Args:
    shape: a Tensor representing the shape of the output Tensor
    axis: an integer

  Returns:
    A tensor with shape shape and type tf.int32, where each elements its
    coordinate along the given axis.
  """

  r = tf.range(shape[axis])
  r_shape = tf.one_hot(
      axis, tf.size(shape), on_value=-1, off_value=1, dtype=tf.int32)
  return tf.zeros(shape, dtype=tf.int32) + tf.reshape(r, r_shape)


def self_attention_expert(
    x,
    batch_coordinate,
    mask_right=True,
    split_batch=False,
    attention_num_head=1,
    attention_kq_size=None,
    attention_v_size=None,
):
  """Implementing attention that runs inside each expert.

  Args:
    x: A tensor of shape[batch, depth]. Contains representations from
      different positions, which are lexicographically ordered.
    batch_coordinate: A tensor of shape [batch, 1] containing the batch
      coordinate of each element in x. This is needed to make sure that
      positions from different sequences don't attend to each other.
    mask_right: A bool. If true, we will not attend to positions on the right,
      just as decoder self attention.
    split_batch (bool): If True, each sequence of the batch is processed
      individually on a loop. If False, the sequences are processed all at
      once and a mask is applied to isolate the sequences from each others
    attention_num_head (int): number of attention heads
    attention_kq_size (int): dimension used for the attention key, and query
    attention_v_size (int): dimension used for the attention value

  Returns:
    out: A tensor of shape [batch, depth].
  example use:
  expert_utils.local_moe(
     ...
     expert_fn=functools.partial(self_attention_expert, mask_right=)
     )
  """

  depth = x.get_shape().as_list()[-1]
  length = tf.shape(batch_coordinate)[0]

  # Print a warning message if one of the expert isn't used (useful at
  # inference where summaries aren't used and the gating function don't add
  # noise)
  global _expert_count  # Hack to make each expert have a unique id
  _expert_count += 1
  length = tf.cond(
      tf.equal(length, 0),
      lambda: tf.Print(  # pylint: disable=g-long-lambda
          length, [length], "Expert {} empty: ".format(_expert_count)),
      lambda: length,
  )

  tf.summary.scalar("batch_size", length, family="experts_stats_batch_size")

  attention_kq_size = attention_kq_size or depth
  attention_v_size = attention_v_size or depth

  def length_not_null(x, batch_coordinate):
    """Branch of the graph only evaluated when length isn't null."""

    # Mask between the sequences (not used if map_ids is used)
    bias_batch = attention_bias_coordinates(batch_coordinate)

    def add_or_set_if(prev_bias, new_bias, condition):
      """Add the bias together while concidering the None case."""
      if not condition:
        return prev_bias
      elif prev_bias is None:
        return new_bias
      else:
        return prev_bias + new_bias

    def mask_and_call_attention(x):
      """Function applied once for each sequence of the batch."""

      # Mask to prevent sequences of attenting to the future
      length = tf.shape(x)[1]  # x has shape [1, length,...]
      bias_past = tf.reshape(
          attention_bias_lower_triangle(length), [length, length])
      # bias has shape [length, length]

      bias = None
      bias = add_or_set_if(bias, bias_past, mask_right)
      bias = add_or_set_if(bias, bias_batch, not split_batch)
      bias = tf.reshape(bias, [1, 1, length, length])

      return multihead_attention(
          x,
          None,
          bias,
          total_key_depth=attention_kq_size,
          total_value_depth=attention_v_size,
          output_depth=depth,
          num_heads=attention_num_head,
          dropout_rate=0.0)

    if split_batch:
      out = expert_utils.map_ids(x, batch_coordinate, mask_and_call_attention)
    else:
      x = tf.reshape(x, [1, length, depth])
      out = mask_and_call_attention(x)
      out = tf.squeeze(out, 0)
    return out

  # If the length is empty, just forward an empty tensor (avoid having to
  # evaluate multihead_attention with tensor having dim equal to zeros)
  out = tf.cond(
      tf.equal(length, 0),
      lambda: tf.zeros(shape=[0, depth], dtype=tf.float32, name="empty_out"),
      lambda: length_not_null(x, batch_coordinate),
  )
  return out


def local_expert_attention(
    x,
    k,
    loss_coef,
    attention_num_experts,
    train=True,
    batch_coordinate=None,
    **kwargs
):
  """Attention using a mixture of experts.

    Positions sent to the same expert can attend to each other.
    The mixture of experts is "local" in that it is replicated on each
    datashard.

    local_moe flatten all batches so to avoid problems with padding (ex: all
    padding going to the same expert, self attention attending to non null
    padding tokens,...), the padding should be removed before.

  Args:
    x: a Tensor with shape [batch, length, depth] or [1, batch*length, depth]
    k: The number of experts to dispatch each example to
    loss_coef: a scalar. A multiplier for the expert loss
    attention_num_experts: The number of experts to use
    train: a boolean for the current mode
    batch_coordinate (tf.Tensor): int32 tensor of shape [1, batch*length, 1]
      containing the batch ids. If None, deduced from first dim of x.
    **kwargs: Arguments to forward to self_attention_expert

  Returns:
    y: a Tensor with shape [batch, length, depth]
    loss: a Scalar
  """
  if batch_coordinate is None:
    batch_coordinate = tf.expand_dims(
        coordinate_tensor(tf.shape(x)[:-1], axis=0), axis=-1)
  with tf.variable_scope("local_expert_attention"):
    additional_dispatch_params = {
        "batch_coordinate": batch_coordinate
    }
    return expert_utils.local_moe(
        x,
        train,
        functools.partial(self_attention_expert, **kwargs),
        attention_num_experts,
        k=k,
        loss_coef=loss_coef,
        pass_x=True,
        pass_gates=False,
        additional_dispatch_params=additional_dispatch_params,
    )


@expert_utils.add_name_scope()
def sparse_dot_product_attention(q, k, v, bc, experts_params):
  """Sparse multihead self attention.

  Perform an approximation of the full multihead attention by dispatching
  the tokens using their keys/values. Thus the attention matrix are only
  computed each times on a subset of the tokens.

  Notes:
   * The function don't perform scaling here (multihead_attention does
  the /sqrt(depth)).
   * The padding should have been removed (so batch size should be 1 but length
   contains the elements from all different batches)
   * Right now, only self attention is supported so length_q and length_kv
   should be identical and the function will add triangular mask.
   * The bias is added inside this function to prevent attention to the future.

  Args:
    q (tf.Tensor): Queries of shape [1, heads, length_q, depth_k]
    k (tf.Tensor): Keys of shape [1, heads, length_q, depth_k]
    v (tf.Tensor): Values of shape [1, heads, length_kv, depth_v]
    bc (tf.Tensor): Batch coordinates of shape [1, length_q, 1]
    experts_params (dict): Additional params for the local expert

  Returns:
    tf.Tensor: Approximation of Softmax(Q.K) * V, of shape
      [1, heads, length_q, depth_v]
  """

  assert q.get_shape().as_list()[0] == 1
  assert k.get_shape().as_list()[0] == 1
  assert v.get_shape().as_list()[0] == 1

  @expert_utils.add_name_scope()
  def unpack_heads(x):
    # Flatten the batch. squeeze works because batch_size = 1 (otherwise could
    # use tf.transpose and flatten after unpacking)
    x = tf.squeeze(x, axis=0)
    list_x = tf.unstack(x)
    return list_x  # list[tf.Tensor(shape=[batch * length, depth])]

  bc = tf.squeeze(bc, axis=0)
  list_q = unpack_heads(q)
  list_k = unpack_heads(k)
  list_v = unpack_heads(v)

  @expert_utils.add_name_scope()
  def expert_dot_product(x, q, k, v, bc):
    """Perform dot product on a subset of the sequence.

    Args:
      x (tf.Tensor): Unused but forwarded by local_moe
      q (tf.Tensor): Queries of shape [length_expert, depth_k]
      k (tf.Tensor): Queries of shape [length_expert, depth_k]
      v (tf.Tensor): Queries of shape [length_expert, depth_v]
      bc (tf.Tensor): Batch coordinates of shape [length_expert, 1]

    Returns:
      tf.Tensor: dot product attention output ([length_expert, depth_v])
    """
    length = tf.shape(x)[0]

    # Mask between the sequences
    bias_batch = attention_bias_coordinates(bc)
    # Mask to prevent sequences of attenting to the future
    bias_past = tf.reshape(
        attention_bias_lower_triangle(length), [length, length])
    bias = bias_batch + bias_past  # bias has shape [length, length]
    bias = tf.reshape(bias, [1, 1, length, length])

    # Restore batch and head dimension
    q, k, v = [tf.expand_dims(tf.expand_dims(t, 0), 0) for t in (q, k, v)]
    # Softmax(Q.K)*V
    v_out = dot_product_attention(q, k, v, bias=bias)
    # Remove batch and head dimension
    v_out = tf.squeeze(v_out, axis=0)
    v_out = tf.squeeze(v_out, axis=0)
    return v_out

  list_v_out = []
  total_loss = 0.0
  for q, k, v in zip(list_q, list_k, list_v):
    # Each head get its own dispatcher

    # TODO(epot): Choose which dispatcher use here on the k/q pair (either
    # noisy_top_k_gating or Locality-sensitive hashing)

    # Concatenate along the depth axis
    x = tf.concat([q, k], axis=-1)  # Works because q and k lengths are the same

    # Compute the attention on the sparse tokens
    v_out, loss = expert_utils.local_moe(
        x=x,
        expert_fn=expert_dot_product,
        additional_dispatch_params=dict(
            q=q,
            k=k,
            v=v,
            bc=bc
        ),
        **experts_params
    )
    list_v_out.append(v_out)
    total_loss += loss

  # Restore original shape as expected by multihead_attention
  v_out = tf.stack(list_v_out)  # Merge heads
  v_out = tf.expand_dims(v_out, axis=0)
  return v_out, total_loss / len(list_v_out)


def scaled_dot_product_attention_simple(q, k, v, bias, name=None):
  """scaled dot-product attention.  One head.  One spatial dimension.

  Args:
    q: a Tensor with shape [batch, length_q, depth_k]
    k: a Tensor with shape [batch, length_kv, depth_k]
    v: a Tensor with shape [batch, length_kv, depth_v]
    bias: optional Tensor broadcastable to [batch, length_q, length_kv]
    name: an optional string

  Returns:
    A Tensor.
  """
  with tf.variable_scope(
      name, default_name="scaled_dot_product_attention_simple"):
    scalar = tf.rsqrt(tf.to_float(tf.shape(q)[2]))
    logits = tf.matmul(q * scalar, k, transpose_b=True)
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    return tf.matmul(weights, v)


_function_cache = {}


def multihead_self_attention_memory_efficient(x,
                                              bias,
                                              num_heads,
                                              head_size=None,
                                              epsilon=1e-6,
                                              forget=True,
                                              test_vars=None,
                                              name=None):
  """Multihead scaled-dot-product self-attention.

  Includes layer norm.

  Returns multihead-self-attention(layer_norm(x))

  Computes one attention head at a time to avoid exhausting memory.

  If forget=True, then forget all forwards activations and recompute on
  the backwards pass.

  Args:
    x: a Tensor with shape [batch, length, input_size]
    bias: an attention bias tensor broadcastable to [batch, 1, length, length]
    num_heads: an integer
    head_size: an optional integer - defaults to input_size/num_heads
    epsilon: a float, for layer norm
    forget: a boolean - forget forwards activations and recompute on backprop
    test_vars: optional tuple of variables for testing purposes
    name: an optional string

  Returns:
    A Tensor.
  """
  io_size = x.get_shape().as_list()[-1]
  if head_size is None:
    assert io_size % num_heads == 0
    head_size = io_size / num_heads

  def forward_internal(x, wqkv, wo, attention_bias, norm_scale, norm_bias):
    """Forward function."""
    n = common_layers.layer_norm_compute_python(
        x, epsilon, norm_scale, norm_bias)
    wqkv_split = tf.unstack(wqkv, num=num_heads)
    wo_split = tf.unstack(wo, num=num_heads)
    y = 0
    for h in xrange(num_heads):
      with tf.control_dependencies([y] if h > 0 else []):
        combined = tf.nn.conv1d(n, wqkv_split[h], 1, "SAME")
        q, k, v = tf.split(combined, 3, axis=2)
        o = scaled_dot_product_attention_simple(q, k, v, attention_bias)
        y += tf.nn.conv1d(o, wo_split[h], 1, "SAME")
    return y

  key = ("multihead_self_attention_memory_efficient %s %s" %
         (num_heads, epsilon))
  if not forget:
    forward_fn = forward_internal
  elif key in _function_cache:
    forward_fn = _function_cache[key]
  else:
    @function.Defun(compiled=True)
    def grad_fn(x, wqkv, wo, attention_bias, norm_scale, norm_bias, dy):
      with tf.control_dependencies([dy]):
        n = common_layers.layer_norm_compute_python(
            x, epsilon, norm_scale, norm_bias)
        wqkv_split = tf.unstack(wqkv, num=num_heads)
        wo_split = tf.unstack(wo, num=num_heads)
        deps = []
        dwqkvs = []
        dwos = []
        dn = 0
        for h in xrange(num_heads):
          with tf.control_dependencies(deps):
            combined = tf.nn.conv1d(n, wqkv_split[h], 1, "SAME")
            q, k, v = tf.split(combined, 3, axis=2)
            o = scaled_dot_product_attention_simple(q, k, v, attention_bias)
            partial_y = tf.nn.conv1d(o, wo_split[h], 1, "SAME")
            pdn, dwqkvh, dwoh = tf.gradients(
                ys=[partial_y],
                xs=[n, wqkv_split[h], wo_split[h]],
                grad_ys=[dy])
            dn += pdn
            dwqkvs.append(dwqkvh)
            dwos.append(dwoh)
            deps = [dn, dwqkvh, dwoh]
        dwqkv = tf.stack(dwqkvs)
        dwo = tf.stack(dwos)
        with tf.control_dependencies(deps):
          dx, dnorm_scale, dnorm_bias = tf.gradients(
              ys=[n], xs=[x, norm_scale, norm_bias], grad_ys=[dn])
        return (dx, dwqkv, dwo, tf.zeros_like(attention_bias),
                dnorm_scale, dnorm_bias)

    @function.Defun(grad_func=grad_fn, compiled=True,
                    separate_compiled_gradients=True)
    def forward_fn(x, wqkv, wo, attention_bias, norm_scale, norm_bias):
      return forward_internal(
          x, wqkv, wo, attention_bias, norm_scale, norm_bias)
    _function_cache[key] = forward_fn

  if bias is not None:
    bias = tf.squeeze(bias, 1)
  with tf.variable_scope(name, default_name="multihead_attention", values=[x]):
    # TODO(noam): it would be nice to save memory by casting x to float16
    # here, but this causes problems with the gradients.  Figure out if there
    # is a way to leave the gradients as float32.
    if test_vars is not None:
      wqkv, wo, norm_scale, norm_bias = list(test_vars)
    else:
      wqkv = tf.get_variable(
          "wqkv", [num_heads, 1, io_size, 3 * head_size],
          initializer=tf.random_normal_initializer(stddev=io_size**-0.5))
      wo = tf.get_variable(
          "wo", [num_heads, 1, head_size, io_size],
          initializer=tf.random_normal_initializer(
              stddev=(head_size * num_heads)**-0.5))
      norm_scale, norm_bias = common_layers.layer_norm_vars(io_size)
    y = forward_fn(x, wqkv, wo, bias, norm_scale, norm_bias)
    y.set_shape(x.get_shape())
    return y


multihead_attention_sparse_dot_prod = functools.partial(
    multihead_attention, attention_type=sparse_dot_product_attention)
