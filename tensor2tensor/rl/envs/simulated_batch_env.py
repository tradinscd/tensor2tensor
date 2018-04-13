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

import gym

import pkg_resources

from tensor2tensor.rl.envs.in_graph_batch_env import InGraphBatchEnv
from tensor2tensor.utils import registry
from tensor2tensor.utils import trainer_lib

import tensorflow as tf


flags = tf.flags
FLAGS = flags.FLAGS


class SimulatedBatchEnv(InGraphBatchEnv):
  """Batch of environments inside the TensorFlow graph.

  The batch of environments will be stepped and reset inside of the graph using
  a tf.py_func(). The current batch of observations, actions, rewards, and done
  flags are held in according variables.
  """

  def __init__(self, length, observ_shape, observ_dtype, action_shape,
               action_dtype):
    """Batch of environments inside the TensorFlow graph."""
    self.length = length
    hparams = trainer_lib.create_hparams(
        FLAGS.hparams_set, problem_name=FLAGS.problems, data_dir="UNUSED")
    hparams.force_full_predict = True
    self._model = registry.model(FLAGS.model)(
        hparams, tf.estimator.ModeKeys.PREDICT)

    self.action_shape = action_shape
    self.action_dtype = action_dtype

    with open(pkg_resources.resource_filename(
        "tensor2tensor.rl.envs", "frame1.png"), "rb") as f:
      png_frame_1_raw = f.read()

    with open(pkg_resources.resource_filename(
        "tensor2tensor.rl.envs", "frame2.png"), "rb") as f:
      png_frame_2_raw = f.read()

    self.frame_1 = tf.expand_dims(tf.cast(tf.image.decode_png(png_frame_1_raw),
                                          tf.float32), 0)
    self.frame_2 = tf.expand_dims(tf.cast(tf.image.decode_png(png_frame_2_raw),
                                          tf.float32), 0)

    shape = (self.length,) + observ_shape
    self._observ = tf.Variable(tf.zeros(shape, observ_dtype), trainable=False)
    self._prev_observ = tf.Variable(tf.zeros(shape, observ_dtype),
                                    trainable=False)
    self._starting_observ = tf.Variable(tf.zeros(shape, observ_dtype),
                                        trainable=False)

    observ_dtype = tf.int64
    self._observ_not_sure_why_we_need_this = tf.Variable(
        tf.zeros((self.length,) + observ_shape, observ_dtype),
        name="observ_new", trainable=False)

    self._reward_not_sure_why_we_need_this = tf.Variable(
        tf.zeros((self.length, 1), observ_dtype),
        name="reward_new", trainable=False)

  @property
  def action_space(self):
    return gym.make("PongNoFrameskip-v4").action_space

  def __len__(self):
    """Number of combined environments."""
    return self.length

  def simulate(self, action):
    with tf.name_scope("environment/simulate"):
      inputs = {"inputs_0": self._prev_observ.read_value(),
                "inputs_1": self._observ.read_value(),
                "action": action,
                "targets": self._observ_not_sure_why_we_need_this,
                "reward": self._reward_not_sure_why_we_need_this}
      model_output = self._model(inputs)
      observ_expaned = model_output[0]["targets"]
      observ = tf.cast(tf.argmax(observ_expaned, axis=-1), tf.float32)
      reward = tf.constant(0, tf.float32, shape=(self.length,))
      done = tf.constant(False, tf.bool, shape=(self.length,))

      with tf.control_dependencies([observ]):
        with tf.control_dependencies([self._prev_observ.assign(self._observ)]):
          with tf.control_dependencies([self._observ.assign(observ)]):
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
    observ = tf.gather(self._observ, indices)
    observ = 0.0 * tf.check_numerics(observ, "observ")
    with tf.control_dependencies([
        tf.scatter_update(self._observ, indices, observ + self.frame_2),
        tf.scatter_update(self._prev_observ, indices, observ + self.frame_1)]):
      return tf.identity(self._observ.read_value())

  @property
  def observ(self):
    """Access the variable holding the current observation."""
    return self._observ
