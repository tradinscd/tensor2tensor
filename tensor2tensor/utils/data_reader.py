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

"""Data reader module."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random

# Dependency imports

import numpy as np

import six
from six.moves import zip  # pylint: disable=redefined-builtin

from tensor2tensor.data_generators import problem_hparams
from tensor2tensor.data_generators.problem import preprocess_examples_common
from tensor2tensor.utils import registry

import tensorflow as tf


def examples_reader(data_sources,
                    data_fields_to_features,
                    training,
                    capacity=32,
                    data_items_to_decoders=None,
                    data_items_to_decode=None):
  """Reads Examples from data_sources and decodes to Tensors.

  The dictionary data_fields_to_features for an image dataset can be:

  data_fields_to_features = {
    'image/encoded': tf.FixedLenFeature((), tf.string, default_value=''),
    'image/format': tf.FixedLenFeature((), tf.string, default_value='raw'),
    'image/class/label': tf.FixedLenFeature(
        [1], tf.int64, default_value=tf.zeros([1], dtype=tf.int64)),
  }

  and for a simple algorithmic dataset with variable-length data it is:

  data_fields_to_features = {
    'inputs': tf.VarLenFeature(tf.int64),
    'targets': tf.VarLenFeature(tf.int64),
  }

  The data_items_to_decoders dictionary argument can be left as None if there
  is no decoding to be performed. But, e.g. for images, it should be set so that
  the images are decoded from the features, e.g., for MNIST:

  data_items_to_decoders = {
    'image': tfexample_decoder.Image(
      image_key = 'image/encoded',
      format_key = 'image/format',
      shape=[28, 28],
      channels=1),
    'label': tfexample_decoder.Tensor('image/class/label'),
  }

  These arguments are compatible with the use of tf.contrib.slim.data module,
  see there for more documentation.

  Args:
    data_sources: a list or tuple of sources from which the data will be read,
      for example [/path/to/train@128, /path/to/train2*, /tmp/.../train3*]
    data_fields_to_features: a dictionary from data fields in the data sources
      to features, such as tf.VarLenFeature(tf.int64), see above for examples.
    training: a Boolean, whether to read for training or evaluation.
    capacity: integer, buffer capacity; set to 2 * max_batch_size or more.
    data_items_to_decoders: a dictionary mapping data items (that will be
      in the returned result) to decoders that will decode them using features
      defined in data_fields_to_features; see above for examples. By default
      (if this is None), we grab the tensor from every feature.
    data_items_to_decode: a subset of data items that will be decoded;
      by default (if this is None), we decode all items.

  Returns:
    A tf.contrib.data.Dataset of dict<feature name, Tensor>
  """

  def decode_record(record):
    """Serialized Example to dict of <feature name, Tensor>."""
    example_serialized = record
    item_decoders = data_items_to_decoders
    if item_decoders is None:
      item_decoders = {
          field: tf.contrib.slim.tfexample_decoder.Tensor(field)
          for field in data_fields_to_features
      }

    decoder = tf.contrib.slim.tfexample_decoder.TFExampleDecoder(
        data_fields_to_features, item_decoders)

    decode_items = data_items_to_decode
    if decode_items is None:
      decode_items = list(item_decoders)

    decoded = decoder.decode(example_serialized, items=decode_items)
    return dict(zip(decode_items, decoded))

  with tf.name_scope("examples_in"):
    data_files = tf.contrib.slim.parallel_reader.get_data_files(data_sources)
    if training:
      random.shuffle(data_files)
    dataset = tf.contrib.data.TFRecordDataset(data_files)
    num_threads = min(4 if training else 1, len(data_files))
    dataset = dataset.map(decode_record, num_threads=num_threads)
    if training:
      dataset = dataset.shuffle(capacity)
    # Loop inifinitely if training, just once otherwise
    dataset = dataset.repeat(None if training else 1)
    return dataset


def preprocessing(examples, data_file_pattern):
  """Preprocessing of examples."""
  # This function is for obsolete problems only, as we're porting them
  # all to the Problem class and its preprocess_examples method. Don't add.
  if "image" in data_file_pattern:

    def resize(img, size):
      return tf.to_int64(
          tf.image.resize_images(img, [size, size], tf.image.ResizeMethod.AREA))

    if "img2img" in data_file_pattern:
      inputs = examples["inputs"]
      examples["inputs"] = resize(inputs, 16)
      examples["targets"] = resize(inputs, 64)
    elif "image_celeba" in data_file_pattern:
      inputs = examples["inputs"]
      # Remove boundaries in CelebA images. Remove 40 pixels each side
      # vertically and 20 pixels each side horizontally.
      inputs = tf.image.crop_to_bounding_box(inputs, 40, 20, 218 - 80, 178 - 40)
      examples["inputs"] = resize(inputs, 8)
      examples["targets"] = resize(inputs, 32)
  elif "audio" in data_file_pattern:
    # Reshape audio to proper shape
    sample_count = tf.to_int32(examples.pop("audio/sample_count"))
    sample_width = tf.to_int32(examples.pop("audio/sample_width"))
    channel_count = 1
    examples["inputs"] = tf.reshape(examples["inputs"],
                                    [sample_count, sample_width, channel_count])
    if "wsj" in data_file_pattern:
      examples["inputs"] = tf.bitcast(examples["inputs"], tf.int32)
  elif "a2q_20161229" in data_file_pattern:
    # we forgot the EOS when we preprocessed this data.
    examples["targets"] = tf.concat([examples["targets"], [1]], 0)
  return examples


def cast_int64_to_int32(features):
  f = {}
  for k, v in six.iteritems(features):
    if v.dtype == tf.int64:
      v = tf.to_int32(v)
    f[k] = v
  return f


def feature_placeholders(data_fields):
  feature_map = {}
  for (field, tp) in data_fields:
    if not field.startswith("targets"):
      feature_map[field] = tf.placeholder(
          dtype=tp, shape=[None] * 4, name=field)
  return feature_map


def default_example_reading_spec(data_file_pattern):
  """Example reading spec for problem_hparams problems."""
  # This function is for problems that have yet to be ported to the new Problem
  # API. Do not add here.
  data_items_to_decoders = None
  # Read from image TFRecords if the file has "image" in its name.
  if data_file_pattern and "image" in data_file_pattern:
    label_key = "image/class/label"
    data_fields = {
        "image/encoded": tf.FixedLenFeature((), tf.string),
        "image/format": tf.FixedLenFeature((), tf.string),
        label_key: tf.VarLenFeature(tf.int64)
    }
    data_items_to_decoders = {
        "inputs":
            tf.contrib.slim.tfexample_decoder.Image(
                image_key="image/encoded",
                format_key="image/format",
                channels=1 if "mnist" in data_file_pattern else 3),
        "targets":
            tf.contrib.slim.tfexample_decoder.Tensor(label_key),
    }
  elif data_file_pattern and "audio" in data_file_pattern:
    data_type = tf.int64 if "timit" in data_file_pattern else tf.float32
    data_fields = {
        "inputs": tf.VarLenFeature(data_type),
        "audio/sample_count": tf.FixedLenFeature((), tf.int64),
        "audio/sample_width": tf.FixedLenFeature((), tf.int64),
        "targets": tf.VarLenFeature(tf.int64),
    }
  else:
    data_fields = {
        "inputs": tf.VarLenFeature(tf.int64),
        "targets": tf.VarLenFeature(tf.int64)
    }
  return data_fields, data_items_to_decoders


def input_pipeline(problem, data_file_pattern, capacity, mode, hparams,
                   batching_scheme):
  """Input pipeline, returns a dictionary of tensors from queues.

  Args:
    problem: Problem instance for which to build the input pipeline.
    data_file_pattern: file pattern for input files.
    capacity: int, data pipeline buffer capacity.
    mode: tf.contrib.learn.ModeKeys entry.
    hparams: an HParams object.
    batching_scheme: a dictionary containing
      "boundaries": a list of integers for the boundaries that will be
        used for bucketing; see tf.contrib.training.bucket_by_sequence_length
        for more details.
      "batch_sizes": a list of batch sizes corresponding to the buckets
      "max_length": an integer.  We drop sequences which are longer.

  Returns:
    dict <feature name, batched and padded Tensor>
  """
  with tf.name_scope("input_pipeline"):
    if problem is None:
      data_fields, data_items_to_decoders = default_example_reading_spec(
          data_file_pattern)
    else:
      data_fields, data_items_to_decoders = problem.example_reading_spec()

    if data_file_pattern is None:
      # Create placeholders for input, rather than reading data from disk.
      return feature_placeholders(data_fields)

    is_training = mode == tf.contrib.learn.ModeKeys.TRAIN
    dataset = examples_reader(
        [data_file_pattern],
        data_fields,
        training=is_training,
        capacity=capacity,
        data_items_to_decoders=data_items_to_decoders)

    def example_len(ex):
      length = 0
      # The queue to bucket on will be chosen based on maximum length.
      for v in ex.values():
        # For images the sequence length is the size of the spatial dimensions.
        sequence_length = (tf.shape(v)[0] if len(v.get_shape()) < 3 else
                           tf.shape(v)[0] * tf.shape(v)[1])
        length = tf.maximum(length, sequence_length)
      return length

    def preprocess(example):
      """Preprocessing for example."""
      if problem is None:
        example = preprocess_examples_common(example, hparams)
        example = preprocessing(example, data_file_pattern)
      else:
        example = problem.preprocess_examples(example, mode, hparams)

      # We do not want int64s as they are not supported on GPUs.
      example = cast_int64_to_int32(example)

      return example

    def example_to_bucket_id(example):
      """Return int64 id of the length bucket for this example."""
      seq_length = example_len(example)

      # From tf.contrib.training.bucket_by_sequence_length:
      boundaries = list(batching_scheme["boundaries"])
      buckets_min = [np.iinfo(np.int32).min] + boundaries
      buckets_max = boundaries + [np.iinfo(np.int32).max]
      conditions_c = tf.logical_and(
          tf.less_equal(buckets_min, seq_length),
          tf.less(seq_length, buckets_max))
      bucket_id = tf.reduce_min(tf.where(conditions_c))

      return bucket_id

    def batching_fn(bucket_id, grouped_dataset):
      batch_sizes = tf.constant(batching_scheme["batch_sizes"], dtype=tf.int64)
      batch_size = batch_sizes[bucket_id]

      # Pad each dimension of each feature so that they match.
      padded_shapes = dict(
          [(name, [None] * len(shape))
           for name, shape in grouped_dataset.output_shapes.items()])
      return grouped_dataset.padded_batch(batch_size, padded_shapes)

    def allowed_length(ex):
      return tf.less_equal(example_len(ex), batching_scheme["max_length"])

    num_threads = 4 if is_training else 1
    dataset = dataset.map(preprocess, num_threads=num_threads)
    dataset = dataset.filter(allowed_length)

    window_size = max(batching_scheme["batch_sizes"])
    dataset = dataset.group_by_window(example_to_bucket_id, batching_fn,
                                      window_size)

    batched_examples = dataset.make_one_shot_iterator().get_next()
    return batched_examples


def bucket_boundaries(max_length, min_length=8, mantissa_bits=2):
  """A default set of length-bucket boundaries."""
  x = min_length
  boundaries = []
  while x < max_length:
    boundaries.append(x)
    x += 2**max(0, int(math.log(x, 2)) - mantissa_bits)
  return boundaries


def hparams_to_batching_scheme(hparams,
                               drop_long_sequences=False,
                               shard_multiplier=1,
                               length_multiplier=1):
  """A batching scheme based on model hyperparameters.

  Every batch containins a number of sequences divisible by `shard_multiplier`.

  If `drop_long_sequences` is True, then sequences longer than
  `hparams.batch_size` are dropped.  This prevents generating batches with
  more than the usual number of tokens, which can cause out-of-memory errors.

  Args:
    hparams: a hyperparameters.
    drop_long_sequences: a boolean.
    shard_multiplier: an integer increasing the batch_size to suit splitting
      across datashards.
    length_multiplier: an integer multiplier that is used to increase the
      batch sizes and sequence length tolerance.

  Returns:
     a dictionary
  """
  max_length = hparams.max_length or hparams.batch_size
  boundaries = bucket_boundaries(
      max_length, mantissa_bits=hparams.batching_mantissa_bits)
  batch_sizes = [
      max(1, hparams.batch_size // length)
      for length in boundaries + [max_length]
  ]
  batch_sizes = [b * shard_multiplier for b in batch_sizes]
  max_length *= length_multiplier
  boundaries = [boundary * length_multiplier + 1 for boundary in boundaries]
  return {
      "boundaries": boundaries,
      "batch_sizes": batch_sizes,
      "max_length": (max_length if drop_long_sequences else 10**9)
  }


def constant_batching_scheme(constant_batch_size_in_sequences):
  """A batching scheme with constant batch size.

  Args:
    constant_batch_size_in_sequences: an integer

  Returns:
     a dictionary
  """
  boundaries = bucket_boundaries(1024)
  batch_sizes = [constant_batch_size_in_sequences] * (1 + len(boundaries))
  return {
      "boundaries": boundaries,
      "batch_sizes": batch_sizes,
      "max_length": 10**9
  }


def get_data_filepatterns(problems, data_dir, mode):
  """Return the location of a dataset for a given mode."""
  datasets = []
  for problem in problems.split("-"):
    try:
      problem = registry.problem(problem).dataset_filename()
    except ValueError:
      problem, _, _ = problem_hparams.parse_problem_name(problem)
    path = os.path.join(data_dir, problem)
    if mode == tf.contrib.learn.ModeKeys.TRAIN:
      datasets.append("%s-train*" % path)
    else:
      datasets.append("%s-dev*" % path)
  return datasets
