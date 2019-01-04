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

"""Data generators for the Question-Answering NLI dataset."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import zipfile
from tensor2tensor.data_generators import generator_utils
from tensor2tensor.data_generators import problem
from tensor2tensor.data_generators import text_encoder
from tensor2tensor.data_generators import text_problems
from tensor2tensor.utils import registry
import tensorflow as tf

EOS = text_encoder.EOS


@registry.register_problem
class QuestionNLI(text_problems.TextConcat2ClassProblem):
  """Question Answering NLI classification problems."""

  # Link to data from GLUE: https://gluebenchmark.com/tasks
  _QNLI_URL = ("https://firebasestorage.googleapis.com/v0/b/"
               "mtl-sentence-representations.appspot.com/o/"
               "data%2FQNLI.zip?alt=media&token=c24cad61-f2df-"
               "4f04-9ab6-aa576fa829d0")

  @property
  def is_generate_per_split(self):
    return True

  @property
  def dataset_splits(self):
    return [{
        "split": problem.DatasetSplit.TRAIN,
        "shards": 100,
    }, {
        "split": problem.DatasetSplit.EVAL,
        "shards": 1,
    }]

  @property
  def approx_vocab_size(self):
    return 2**15

  @property
  def num_classes(self):
    return 2

  def class_labels(self, data_dir):
    del data_dir
    # Note this binary classification is different from usual MNLI.
    return ["not_entailment", "entailment"]

  def _maybe_download_corpora(self, tmp_dir):
    qnli_filename = "QNLI.zip"
    qnli_finalpath = os.path.join(tmp_dir, "QNLI")
    if not tf.gfile.Exists(qnli_finalpath):
      zip_filepath = generator_utils.maybe_download(
          tmp_dir, qnli_filename, self._QNLI_URL)
      zip_ref = zipfile.ZipFile(zip_filepath, "r")
      zip_ref.extractall(tmp_dir)
      zip_ref.close()

    return qnli_finalpath

  def example_generator(self, filename):
    label_list = self.class_labels(data_dir=None)
    for idx, line in enumerate(tf.gfile.Open(filename, "rb")):
      if idx == 0: continue  # skip header
      line = text_encoder.to_unicode_utf8(line.strip())
      _, s1, s2, l = line.split("\t")
      inputs = [s1, s2]
      l = label_list.index(l)
      yield {
          "inputs": inputs,
          "label": l
      }

  def generate_samples(self, data_dir, tmp_dir, dataset_split):
    qnli_dir = self._maybe_download_corpora(tmp_dir)
    if dataset_split == problem.DatasetSplit.TRAIN:
      filesplit = "train.tsv"
    else:
      filesplit = "dev.tsv"

    filename = os.path.join(qnli_dir, filesplit)
    for example in self.example_generator(filename):
      yield example


@registry.register_problem
class QuestionNLICharacters(QuestionNLI):
  """Question-Answering NLI classification problems, character level"""

  @property
  def vocab_type(self):
    return text_problems.VocabType.CHARACTER

  def global_task_id(self):
    return problem.TaskID.EN_NLI
