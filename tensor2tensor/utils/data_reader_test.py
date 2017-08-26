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

"""Data reader test."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import tempfile

# Dependency imports

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin

from tensor2tensor.data_generators import generator_utils
from tensor2tensor.data_generators import problem as problem_mod
from tensor2tensor.utils import data_reader
from tensor2tensor.utils import registry

import tensorflow as tf


@registry.register_problem
class TestProblem(problem_mod.Problem):

  def generator(self, data_dir, tmp_dir, is_training):
    for i in xrange(30):
      yield {"inputs": [i] * (i + 1), "targets": [i], "floats": [i + 0.5]}

  def generate_data(self, data_dir, tmp_dir, task_id=-1):
    train_paths = self.training_filepaths(data_dir, 1, shuffled=True)
    dev_paths = self.dev_filepaths(data_dir, 1, shuffled=True)
    generator_utils.generate_files(
        self.generator(data_dir, tmp_dir, True), train_paths)
    generator_utils.generate_files(
        self.generator(data_dir, tmp_dir, False), dev_paths)

  def hparams(self, defaults, model_hparams):
    pass

  def example_reading_spec(self):
    data_fields = {
        "inputs": tf.VarLenFeature(tf.int64),
        "targets": tf.VarLenFeature(tf.int64),
        "floats": tf.VarLenFeature(tf.float32),
    }
    data_items_to_decoders = None
    return (data_fields, data_items_to_decoders)

  def preprocess_examples(self, examples, unused_mode, unused_hparams):
    examples["new_field"] = tf.constant([42.42])
    return examples


def generate_test_data(problem, tmp_dir):
  problem.generate_data(tmp_dir, tmp_dir)
  filepatterns = data_reader.get_data_filepatterns(
      problem.name, tmp_dir, tf.contrib.learn.ModeKeys.TRAIN)
  assert tf.gfile.Glob(filepatterns[0])
  return filepatterns


class DataReaderTest(tf.test.TestCase):

  @classmethod
  def setUpClass(cls):
    tf.set_random_seed(1)
    cls.problem = registry.problem("test_problem")
    cls.filepatterns = generate_test_data(cls.problem, tempfile.gettempdir())

  @classmethod
  def tearDownClass(cls):
    # Clean up files
    for fp in cls.filepatterns:
      files = tf.gfile.Glob(fp)
      for f in files:
        os.remove(f)

  def testBasicExampleReading(self):
    dataset = data_reader.read_examples(self.problem, self.filepatterns[0], 32)
    examples = dataset.make_one_shot_iterator().get_next()
    with tf.train.MonitoredSession() as sess:
      # Check that there are multiple examples that have the right fields of the
      # right type (lists of int/float).
      for _ in xrange(10):
        ex_val = sess.run(examples)
        inputs, targets, floats = (ex_val["inputs"], ex_val["targets"],
                                   ex_val["floats"])
        self.assertEqual(np.int64, inputs.dtype)
        self.assertEqual(np.int64, targets.dtype)
        self.assertEqual(np.float32, floats.dtype)
        for field in [inputs, targets, floats]:
          self.assertGreater(len(field), 0)

  def testTrainEvalBehavior(self):
    train_dataset = data_reader.read_examples(self.problem,
                                              self.filepatterns[0], 16)
    train_examples = train_dataset.make_one_shot_iterator().get_next()
    eval_dataset = data_reader.read_examples(
        self.problem,
        self.filepatterns[0],
        16,
        mode=tf.contrib.learn.ModeKeys.EVAL)
    eval_examples = eval_dataset.make_one_shot_iterator().get_next()

    eval_idxs = []
    with tf.train.MonitoredSession() as sess:
      # Train should be shuffled and run through infinitely
      for i in xrange(30):
        self.assertNotEqual(i, sess.run(train_examples)["inputs"][0])

      # Eval should not be shuffled and only run through once
      for i in xrange(30):
        self.assertEqual(i, sess.run(eval_examples)["inputs"][0])
        eval_idxs.append(i)

      with self.assertRaises(tf.errors.OutOfRangeError):
        sess.run(eval_examples)
        # Should never run because above line should error
        eval_idxs.append(30)

      # Ensuring that the above exception handler actually ran and we didn't
      # exit the MonitoredSession context.
      eval_idxs.append(-1)

    self.assertAllEqual(list(range(30)) + [-1], eval_idxs)

  def testPreprocess(self):
    dataset = data_reader.read_examples(self.problem, self.filepatterns[0], 32)
    examples = dataset.make_one_shot_iterator().get_next()
    examples = data_reader._preprocess(examples, self.problem, None, None, None)
    with tf.train.MonitoredSession() as sess:
      ex_val = sess.run(examples)
      # problem.preprocess_examples has been run
      self.assertAllClose([42.42], ex_val["new_field"])

      # int64 has been cast to int32
      self.assertEqual(np.int32, ex_val["inputs"].dtype)
      self.assertEqual(np.int32, ex_val["targets"].dtype)
      self.assertEqual(np.float32, ex_val["floats"].dtype)

  def testLengthFilter(self):
    max_len = 15
    dataset = data_reader.read_examples(self.problem, self.filepatterns[0], 32)
    dataset = dataset.filter(
        lambda ex: data_reader._example_too_big(ex, max_len))
    examples = dataset.make_one_shot_iterator().get_next()
    with tf.train.MonitoredSession() as sess:
      ex_lens = []
      for _ in xrange(max_len):
        ex_lens.append(len(sess.run(examples)["inputs"]))

    self.assertAllEqual(list(range(1, max_len + 1)), sorted(ex_lens))

  def testBatchingSchemeMaxLength(self):
    scheme = data_reader._batching_scheme(
        batch_size=20, max_length=None, drop_long_sequences=False)
    self.assertGreater(scheme["max_length"], 10000)

    scheme = data_reader._batching_scheme(
        batch_size=20, max_length=None, drop_long_sequences=True)
    self.assertEqual(scheme["max_length"], 20)

    scheme = data_reader._batching_scheme(
        batch_size=20, max_length=15, drop_long_sequences=True)
    self.assertEqual(scheme["max_length"], 15)

    scheme = data_reader._batching_scheme(
        batch_size=20, max_length=15, drop_long_sequences=False)
    self.assertGreater(scheme["max_length"], 10000)

  def testBatchingSchemeBuckets(self):
    scheme = data_reader._batching_scheme(batch_size=128)
    boundaries, batch_sizes = scheme["boundaries"], scheme["batch_sizes"]
    self.assertEqual(len(boundaries), len(batch_sizes) - 1)
    expected_boundaries = [8, 12, 16, 24, 32, 48, 64, 96]
    self.assertEqual(expected_boundaries, boundaries)
    expected_batch_sizes = [16, 10, 8, 5, 4, 2, 2, 1, 1]
    self.assertEqual(expected_batch_sizes, batch_sizes)

    scheme = data_reader._batching_scheme(batch_size=128, shard_multiplier=2)
    boundaries, batch_sizes = scheme["boundaries"], scheme["batch_sizes"]
    self.assertAllEqual([bs * 2 for bs in expected_batch_sizes], batch_sizes)
    self.assertEqual(expected_boundaries, boundaries)

    scheme = data_reader._batching_scheme(batch_size=128, length_multiplier=2)
    boundaries, batch_sizes = scheme["boundaries"], scheme["batch_sizes"]
    self.assertAllEqual([b * 2 for b in expected_boundaries], boundaries)
    self.assertEqual([max(1, bs // 2)
                      for bs in expected_batch_sizes], batch_sizes)

  def testBucketBySeqLength(self):

    def example_len(ex):
      return tf.shape(ex["inputs"])[0]

    boundaries = [10, 20, 30]
    batch_sizes = [10, 8, 4, 2]

    dataset = data_reader.read_examples(
        self.problem,
        self.filepatterns[0],
        32,
        mode=tf.contrib.learn.ModeKeys.EVAL)
    dataset = data_reader.bucket_by_sequence_length(dataset, example_len,
                                                    boundaries, batch_sizes)
    batch = dataset.make_one_shot_iterator().get_next()

    input_vals = []
    obs_batch_sizes = []
    with tf.train.MonitoredSession() as sess:
      # Until OutOfRangeError
      while True:
        batch_val = sess.run(batch)
        batch_inputs = batch_val["inputs"]
        batch_size, max_len = batch_inputs.shape
        obs_batch_sizes.append(batch_size)
        for inputs in batch_inputs:
          input_val = inputs[0]
          input_vals.append(input_val)
          # The inputs were constructed such that they were repeated value+1
          # times (i.e. if the inputs value is 7, the example has 7 repeated 8
          # times).
          repeat = input_val + 1
          # Check padding
          self.assertAllEqual([input_val] * repeat + [0] * (max_len - repeat),
                              inputs)

    # Check that all inputs came through
    self.assertEqual(list(range(30)), sorted(input_vals))
    # Check that we saw variable batch size
    self.assertTrue(len(set(obs_batch_sizes)) > 1)


if __name__ == "__main__":
  tf.test.main()
