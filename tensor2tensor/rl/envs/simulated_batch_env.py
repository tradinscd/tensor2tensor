# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
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
"""Batch of environments inside the TensorFlow graph."""

# The code was based on Danijar Hafner's code from tf.agents:
# https://github.com/tensorflow/agents/blob/master/agents/tools/in_graph_batch_env.py

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from tensor2tensor.layers import common_layers
from tensor2tensor.rl.envs import in_graph_batch_env
from tensor2tensor.utils import registry
from tensor2tensor.utils import trainer_lib

import tensorflow as tf


flags = tf.flags
FLAGS = flags.FLAGS


class HistoryBuffer(object):
  """History Buffer."""

  def __init__(self, input_data_iterator, length):
    initial_frames = tf.cast(input_data_iterator.get_next()["inputs"],
                             tf.float32)
    self.initial_frames = tf.stack([initial_frames]*length)
    initial_shape = common_layers.shape_list(self.initial_frames)
    with tf.variable_scope(tf.get_variable_scope(), reuse=tf.AUTO_REUSE):
      self._history_buff = tf.get_variable(
          "history_buffer",
          initializer=tf.zeros(initial_shape, tf.float32),
          trainable=False)
    self._assigned = False

  def get_all_elements(self):
    if self._assigned:
      return self._history_buff.read_value()
    assign = self._history_buff.assign(self.initial_frames)
    with tf.control_dependencies([assign]):
      self._assigned = True
      return tf.identity(self.initial_frames)

  def move_by_one_element(self, element):
    last_removed = self.get_all_elements()[:, 1:, ...]
    element = tf.expand_dims(element, dim=1)
    moved = tf.concat([last_removed, element], axis=1)
    with tf.control_dependencies([moved]):
      with tf.control_dependencies([self._history_buff.assign(moved)]):
        self._assigned = True
        return self._history_buff.read_value()

  def reset(self, indices):
    number_of_indices = tf.size(indices)
    initial_frames = self.initial_frames[:number_of_indices, ...]
    scatter_op = tf.scatter_update(self._history_buff, indices, initial_frames)
    with tf.control_dependencies([scatter_op]):
      self._assigned = True
      return self._history_buff.read_value()


class SimulatedBatchEnv(in_graph_batch_env.InGraphBatchEnv):
  """Batch of environments inside the TensorFlow graph.

  The batch of environments will be stepped and reset inside of the graph using
  a tf.py_func(). The current batch of observations, actions, rewards, and done
  flags are held in according variables.
  """

  def __init__(self, environment_lambda, length, problem):
    """Batch of environments inside the TensorFlow graph."""
    self.length = length
    self._num_frames = problem.num_input_frames

    initialization_env = environment_lambda()
    hparams = trainer_lib.create_hparams(
        FLAGS.hparams_set, problem_name=FLAGS.problem)
    hparams.force_full_predict = True
    self._model = registry.model(FLAGS.model)(
        hparams, tf.estimator.ModeKeys.PREDICT)

    self.action_space = initialization_env.action_space
    self.action_shape = list(initialization_env.action_space.shape)
    self.action_dtype = tf.int32

    dataset = problem.dataset(tf.estimator.ModeKeys.TRAIN, FLAGS.data_dir)
    dataset = dataset.repeat()
    input_data_iterator = dataset.make_one_shot_iterator()

    self.history_buffer = HistoryBuffer(input_data_iterator, self.length)

    shape = (self.length, problem.frame_height, problem.frame_width,
             problem.num_channels)
    with tf.variable_scope(tf.get_variable_scope(), reuse=tf.AUTO_REUSE):
      self._observ = tf.get_variable("observation",
                                     initializer=tf.zeros(shape, tf.float32),
                                     trainable=False)

  def __len__(self):
    """Number of combined environments."""
    return self.length

  def simulate(self, action):
    with tf.name_scope("environment/simulate"):
      actions = tf.concat([tf.expand_dims(action, axis=1)] * self._num_frames,
                          axis=1)
      history = self.history_buffer.get_all_elements()
      with tf.variable_scope(tf.get_variable_scope(), reuse=tf.AUTO_REUSE):
        model_output = self._model.infer(
            {"inputs": history, "input_action": actions})
      observ = model_output["targets"]
      observ = tf.cast(observ[:, 0, :, :, :], tf.float32)
      # TODO(lukaszkaiser): instead of -1 use min_reward in the line below.
      reward = model_output["target_reward"][:, 0, 0, 0] - 1
      reward = tf.cast(reward, tf.float32)
      # Some wrappers need explicit shape, so we reshape here.
      reward = tf.reshape(reward, shape=(self.length,))
      done = tf.constant(False, tf.bool, shape=(self.length,))

      with tf.control_dependencies([observ]):
        with tf.control_dependencies(
            [self._observ.assign(observ),
             self.history_buffer.move_by_one_element(observ)]):
          return tf.identity(reward), tf.identity(done)

  def reset(self, indices=None):
    """Reset the batch of environments.

    Args:
      indices: The batch indices of the environments to reset.

    Returns:
      Batch tensor of the new observations.
    """
    return tf.cond(
        tf.cast(tf.shape(indices)[0], tf.bool),
        lambda: self._reset_non_empty(indices), lambda: 0.0)

  def _reset_non_empty(self, indices):
    """Reset the batch of environments.

    Args:
      indices: The batch indices of the environments to reset; defaults to all.

    Returns:
      Batch tensor of the new observations.
    """
    with tf.control_dependencies([self.history_buffer.reset(indices)]):
      with tf.control_dependencies([self._observ.assign(
          self.history_buffer.get_all_elements()[:, -1, ...])]):
        return tf.identity(self._observ.read_value())

  @property
  def observ(self):
    """Access the variable holding the current observation."""
    return self._observ
