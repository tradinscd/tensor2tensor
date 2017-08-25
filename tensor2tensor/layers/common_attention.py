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
from functools import partial

import math

# Dependency imports

from six.moves import xrange  # pylint: disable=redefined-builtin

from tensor2tensor.layers import common_layers
from tensor2tensor.utils import expert_utils

import tensorflow as tf


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


def attention_bias_lower_triangle(length):
  """Create an bias tensor to be added to attention logits.

  Args:
   length: a Scalar.

  Returns:
    a `Tensor` with shape [1, 1, length, length].
  """
  lower_triangle = tf.matrix_band_part(tf.ones([length, length]), -1, 0)
  ret = -1e9 * (1.0 - lower_triangle)
  return tf.reshape(ret, [1, 1, length, length])


def attention_bias_ignore_padding(memory_padding):
  """Create an bias tensor to be added to attention logits.

  Args:
    memory_padding: a float `Tensor` with shape [batch, memory_length].

  Returns:
    a `Tensor` with shape [batch, 1, 1, memory_length].
  """
  ret = memory_padding * -1e9
  return tf.expand_dims(tf.expand_dims(ret, axis=1), axis=1)


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


def split_heads(x, num_heads):
  """Split channels (dimension 3) into multiple heads (becomes dimension 1).

  Args:
    x: a Tensor with shape [batch, length, channels]
    num_heads: an integer

  Returns:
    a Tensor with shape [batch, num_heads, length, channels / num_heads]
  """
  return tf.transpose(split_last_dimension(x, num_heads), [0, 2, 1, 3])


def combine_heads(x):
  """Inverse of split_heads.

  Args:
    x: a Tensor with shape [batch, num_heads, length, channels / num_heads]

  Returns:
    a Tensor with shape [batch, length, channels]
  """
  return combine_last_two_dimensions(tf.transpose(x, [0, 2, 1, 3]))


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


def dot_product_attention(q,
                          k,
                          v,
                          bias,
                          dropout_rate=0.0,
                          image_shapes=None,
                          name=None):
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
    if not tf.get_variable_scope().reuse:
      attention_image_summary(weights, image_shapes)
    return tf.matmul(weights, v)


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
    depth_k = tf.shape(q)[3]
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

    logits = tf.matmul(q, k_new, transpose_b=True)

    attention = tf.nn.softmax(logits + attention_bias)
    output = tf.matmul(attention, v_new)

    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_shape)
    return output


def local_attention_2d(q,
                       k,
                       v,
                       block_length=128,
                       filter_flange=100,
                       name=None):
  """strided block local self-attention.

  Args:
    q: a Tensor with shape [batch, heads, h, w, depth_k]
    k: a Tensor with shape [batch, heads, h, w, depth_k]
    v: a Tensor with shape [batch, heads, h, w, depth_v]
    block_length: an integer indicating the side length of each square block.
    filter_flange: an integer indicating how much to look around each block.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, h, w, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_self_attention_2d", values=[q, k, v]):
    v_shape = tf.shape(v)
    depth_v = tf.shape(v)[4]
    batch_size = tf.shape(q)[0]
    num_heads = tf.shape(q)[1]
    original_length = tf.shape(q)[2] * tf.shape(q)[3]

    def reshape_range(tensor, i, j, shape):
      """Reshapes a tensor between dimensions i and j."""
      target_shape = tf.concat(
          [tf.shape(tensor)[:i], shape, tf.shape(tensor)[j:]],
          axis=0)
      return tf.reshape(tensor, target_shape)

    def pad_to_multiple(x, d):
      """Making sure x is a multiple of d."""
      height_padding = -tf.shape(x)[1] % d
      width_padding = -tf.shape(x)[2] % d
      paddings = [[0, 0], [0, 0], [0, height_padding],
                  [0, width_padding], [0, 0]]
      return tf.pad(x, paddings)

    def gather_indices(x, block_length, stride):
      """Getting gather indices."""
      # making an identity matrix kernel
      kernel = tf.eye(block_length ** 2)
      kernel = reshape_range(kernel, 0, 1, [block_length, block_length, 1])
      # making indices [1, h, w, 1] to appy convs
      indices = tf.range(0, tf.shape(x)[2] * tf.shape(x)[3], delta=1)
      indices = tf.reshape(indices, [1, tf.shape(x)[2], tf.shape(x)[3], 1])
      indices = tf.nn.conv2d(
          tf.cast(indices, tf.float32),
          kernel,
          strides=[1, stride, stride, 1],
          padding="VALID")
      # making indices [num_blocks, dim] to gather
      num_blocks = tf.reduce_prod(tf.shape(indices)[:2])
      indices = tf.reshape(indices, [num_blocks, -1])
      return tf.cast(indices, tf.int32)

    def gather_blocks(x, indices):
      """Gathers flattened blocks from x."""
      x_shape = tf.shape(x)
      x = reshape_range(x, 2, 4, [tf.reduce_prod(x_shape[2:4])])
      # [length, batch, heads, dim]
      x_t = tf.transpose(x, [2, 0, 1, 3])
      x_new = tf.gather(x_t, indices)
      # returns [batch, heads, num_blocks, block_length ** 2, dim]
      return tf.transpose(x_new, [2, 3, 0, 1, 4])

    q = pad_to_multiple(q, block_length)
    k = pad_to_multiple(k, block_length)
    v = pad_to_multiple(v, block_length)

    # Setting up k and v values
    paddings = [[0, 0], [0, 0], [filter_flange, filter_flange],
                [filter_flange, filter_flange], [0, 0]]
    k = tf.pad(k, paddings)
    v = tf.pad(v, paddings)

    # Setting up q blocks
    q_indices = gather_indices(q, block_length, block_length)
    q_new = gather_blocks(q, q_indices)

    # Setting up k and v blocks
    full_filter_width = block_length + 2 * filter_flange
    k_and_v_indices = gather_indices(k, full_filter_width, block_length)
    k_new = gather_blocks(k, k_and_v_indices)
    v_new = gather_blocks(v, k_and_v_indices)

    attention_bias = tf.expand_dims(
        tf.to_float(embedding_to_padding(k_new)) * -1e9, axis=-2)

    logits = tf.matmul(q_new, k_new, transpose_b=True)

    attention = tf.nn.softmax(logits + attention_bias)
    output = tf.matmul(attention, v_new)

    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])
    # Remove the padding if introduced
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    # [batch, heads, h, w, depth_v]
    return tf.reshape(output, v_shape)


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


def multihead_attention(query_antecedent,
                        memory_antecedent,
                        bias,
                        total_key_depth,
                        total_value_depth,
                        output_depth,
                        num_heads,
                        dropout_rate,
                        image_shapes=None,
                        attention_type="dot_product",
                        block_length=128,
                        block_width=128,
                        q_filter_width=1,
                        kv_filter_width=1,
                        q_padding="VALID",
                        kv_padding="VALID",
                        name=None):
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
    image_shapes: optional tuple of integer scalars.
      see comments for attention_image_summary()
    attention_type: a string, either "dot_product" or "local_mask_right" or
                    "local_unmasked"
    block_length: an integer - relevant for "local_mask_right"
    block_width: an integer - relevant for "local_unmasked"
    q_filter_width: An integer specifying how wide you want the query to be.
    kv_filter_width: An integer specifying how wide you want the keys and values
    to be.
    q_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    kv_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.

    name: an optional string

  Returns:
    A Tensor.

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

    q = split_heads(q, num_heads)
    k = split_heads(k, num_heads)
    v = split_heads(v, num_heads)
    key_depth_per_head = total_key_depth // num_heads
    q *= key_depth_per_head**-0.5
    if attention_type == "dot_product":
      x = dot_product_attention(q, k, v, bias, dropout_rate, image_shapes)
    elif attention_type == "local_mask_right":
      x = masked_local_attention_1d(q, k, v, block_length=block_length)
    else:
      assert attention_type == "local_unmasked"
      x = local_attention_1d(
          q, k, v, block_length=block_length, filter_width=block_width)
    x = combine_heads(x)
    x = common_layers.conv1d(x, output_depth, 1, name="output_transform")
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

  attention_kq_size = attention_kq_size or depth
  attention_v_size = attention_v_size or depth

  def length_not_null(x, batch_coordinate):
    """Branch of the graph only evaluated when length isn't null."""
    with tf.name_scope("expert_mask"):
      batch_coordinate = tf.squeeze(batch_coordinate, 1)
      # Convert to float first because of b/25387198
      batch_coordinate = tf.to_float(batch_coordinate)
      bc_v = tf.expand_dims(batch_coordinate, 1)
      bc_h = tf.expand_dims(batch_coordinate, 0)
      bias = bc_v - bc_h  # Broadcast to create [length, length] mask
      bias = tf.minimum(1.0, tf.abs(bias))  # Theshold non zeros to 1.0
      bias *= -1e9  # Set non zeros to -infinity

    if mask_right:
      bias += tf.reshape(
          attention_bias_lower_triangle(length), [length, length])
    # bias has shape [length, length]
    bias = tf.reshape(bias, [1, 1, length, length])
    x = tf.reshape(x, [1, length, depth])
    out = multihead_attention(x,
                              None,
                              bias,
                              total_key_depth=attention_kq_size,
                              total_value_depth=attention_v_size,
                              output_depth=depth,
                              num_heads=1,
                              dropout_rate=0.0)
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

#  functools.partial(self_attention_expert, mask_right=, depth=)


def local_expert_attention(
    x,
    k,
    loss_coef,
    attention_num_experts,
    train=True,
    **kwargs
):
  """Attention using a mixture of experts.

    Positions sent to the same expert can attend to each other.
    The mixture of experts is "local" in that it is replicated on each
    datashard.

  Args:
    x: a Tensor with shape [batch, length, depth]
    k: The number of experts to dispatch each example to
    loss_coef: a scalar. A multiplier for the expert loss
    attention_num_experts: The number of experts to use
    train: a boolean for the current mode
    **kwargs: Arguments to forward to self_attention_expert

  Returns:
    y: a Tensor with shape [batch, length, depth]
    loss: a Scalar
  """
  with tf.variable_scope("local_expert_attention"):
    additional_dispatch_params = {
        "batch_coordinate": tf.expand_dims(
            coordinate_tensor(tf.shape(x)[:-1], axis=0), axis=-1)
    }
    return expert_utils.local_moe(
        x,
        train,
        partial(self_attention_expert, **kwargs),
        attention_num_experts,
        k=k,
        loss_coef=loss_coef,
        pass_x=True,
        pass_gates=False,
        additional_dispatch_params=additional_dispatch_params)
