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

"""Transformer model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import mesh_tensorflow as mtf
from mesh_tensorflow.transformer import moe
from mesh_tensorflow.transformer import transformer
from mesh_tensorflow.transformer import transformer_layers
from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.layers import modalities
from tensor2tensor.utils import mtf_model
from tensor2tensor.utils import registry

import tensorflow as tf


@registry.register_model
class MtfUnitransformer(mtf_model.MtfModel):
  """Single-stack Transformer (Transformer Decoder) in mesh_tensorflow.

  Can optionally be autoregressive (language generation) or non-autoregressive
  like BERT.
  """

  @property
  def batch_dims(self):
    hparams = self._hparams
    if hparams.outer_batch_size == 0:
      return [mtf.Dimension("batch", hparams.batch_size)]
    else:
      if hparams.batch_size % hparams.outer_batch_size != 0:
        raise ValueError(
            "hparams.outer_batch_size must divide hparams.batch_size")
      return [
          mtf.Dimension("outer_batch", hparams.outer_batch_size),
          mtf.Dimension("inner_batch",
                        hparams.batch_size // hparams.outer_batch_size)]

  @property
  def autoregressive(self):
    return self._hparams.autoregressive

  @property
  def variable_dtype(self):
    return mtf.VariableDType(
        tf.as_dtype(self._hparams.master_dtype),
        tf.as_dtype(self._hparams.slice_dtype),
        tf.as_dtype(self._hparams.activation_dtype))

  @property
  def length_dim(self):
    return mtf.Dimension(
        "length", self._hparams.length or self._hparams.max_length)

  def _import_to_batch_by_length(self, x, name, mesh):
    mtf_shape = mtf.Shape(self.batch_dims + [self.length_dim])
    x = tf.reshape(x, mtf_shape.to_integer_list)
    return mtf.import_fully_replicated(mesh, x, mtf_shape, name=name)

  def _import_feature(self, features, mesh, key):
    """Import a feature from the features dictionary into a mtf.Tensor.

    Args:
      features: a features dictionary
      mesh: a Mesh
      key: a string

    Returns:
      a mtf.Tensor with dtype int32 and shape self.batch_dims + self.length_dim
    """
    if key not in features:
      return None
    x = tf.to_int32(features[key])
    x = common_layers.expand_squeeze_to_nd(x, 2)
    # pad to length
    extra_length = self.length_dim.size - tf.shape(x)[1]
    x = tf.pad(x, [[0, 0], [0, extra_length]])
    mtf_shape = mtf.Shape(self.batch_dims + [self.length_dim])
    x = tf.reshape(x, mtf_shape.to_integer_list)
    return mtf.import_fully_replicated(mesh, x, mtf_shape, name=key)

  def model(self):
    hparams = self._hparams
    if hparams.label_smoothing != 0:
      raise NotImplementedError(
          "Label smoothing not implemented in unitransformer."
          "  Do you really want it?")
    if isinstance(hparams.layer_stack, transformer.LayerStack):
      layer_stack = hparams.layer_stack
    else:
      # hparams.layer_stack is a function for creating a LayerStack
      layer_stack = hparams.layer_stack(hparams)
    if self.autoregressive:
      input_vocab_size = self._targets_vocab_size
    else:
      input_vocab_size = self._inputs_vocab_size
    return transformer.Unitransformer(
        layer_stack=layer_stack,
        d_model=hparams.d_model,
        input_vocab_size=input_vocab_size,
        output_vocab_size=self._targets_vocab_size,
        autoregressive=self.autoregressive,
        max_length=hparams.max_length,
        z_loss=hparams.z_loss)

  def _mtf_model_fn(self, features, mesh):
    self._original_features = features
    hparams = self._hparams
    def import_feature(key):
      return self._import_feature(features, mesh, key)
    targets = import_feature("targets")
    if self.autoregressive:
      inputs = mtf.shift(
          targets, offset=1, dim=self.length_dim, wrap=False)
    else:
      inputs = import_feature("inputs")
      # TODO(noam): options for bert-style masking here?
    sequence_id = import_feature("targets_segmentation")
    model = self.model()
    logits, loss = model.call_simple(
        inputs=inputs,
        targets=targets,
        compute_loss=True,
        mode=hparams.mode,
        variable_dtype=self.variable_dtype,
        sequence_id=sequence_id)
    return logits, loss

  def mtf_model_fn(self, features, mesh):
    logits, loss = self._mtf_model_fn(features, mesh)
    # combine batch dims
    if len(self.batch_dims) > 1:
      combined_batch_dim = mtf.Dimension(
          self.batch_dims[0].name, mtf.Shape(self.batch_dims).size)
      logits = mtf.reshape(
          logits, [combined_batch_dim] + logits.shape.dims[-2:])
    return logits, loss

  @property
  def _targets_vocab_size(self):
    targets_vocab_size = self._problem_hparams.modality[
        "targets"].top_dimensionality
    targets_vocab_size += (-targets_vocab_size) % self._hparams.vocab_divisor
    return targets_vocab_size

  @property
  def _inputs_vocab_size(self):
    inputs_vocab_size = self._problem_hparams.modality[
        "inputs"].top_dimensionality
    inputs_vocab_size += (-inputs_vocab_size) % self._hparams.vocab_divisor
    return inputs_vocab_size

  def sample(self, features, mesh):
    hparams = self._hparams
    model = self.model()
    def import_feature(key):
      return self._import_feature(features, mesh, key)

    if self.autoregressive:
      # Prepare partial targets.
      # In either features["inputs"] or features["targets"].
      # We force the outputs to begin with these sequences.
      partial_targets = import_feature("inputs")
      if partial_targets is None:
        partial_targets = import_feature("targets")
      if partial_targets is None:
        ids_shape = mtf.Shape(self.batch_dims + [self.length_dim])
        partial_targets = mtf.constant(mesh, 0, ids_shape, dtype=tf.int32)
      if hparams.beam_size > 1:
        raise NotImplementedError(
            "Beam search not implemented for unitransformer.")
      return model.sample_autoregressive(
          partial_targets,
          temperature=hparams.sampling_temp,
          variable_dtype=self.variable_dtype)
    else:
      raise ValueError(
          "Don't know how to sample from non-autoregressive unitransformer")


@registry.register_model
class MtfBitransformer(MtfUnitransformer):
  """Encoder-Decoder Transformer in mesh_tensorflow."""

  def model(self):
    hparams = self._hparams
    if isinstance(hparams.encoder_layer_stack, transformer.LayerStack):
      encoder_layer_stack = hparams.encoder_layer_stack
    else:
      encoder_layer_stack = hparams.encoder_layer_stack(hparams)
    if isinstance(hparams.decoder_layer_stack, transformer.LayerStack):
      decoder_layer_stack = hparams.decoder_layer_stack
    else:
      decoder_layer_stack = hparams.decoder_layer_stack(hparams)
    return transformer.Bitransformer(
        encoder_layer_stack=encoder_layer_stack,
        decoder_layer_stack=decoder_layer_stack,
        encoder_d_model=hparams.d_model,
        decoder_d_model=hparams.d_model,
        input_vocab_size=self._inputs_vocab_size,
        output_vocab_size=self._targets_vocab_size,
        max_length=hparams.max_length,
        shared_embedding=hparams.shared_embedding,
        label_smoothing=hparams.label_smoothing,
        z_loss=hparams.z_loss)

  def _mtf_model_fn(self, features, mesh):
    self._original_features = features
    hparams = self._hparams
    def import_feature(key):
      return self._import_feature(features, mesh, key)
    targets = import_feature("targets")
    inputs = import_feature("inputs")
    encoder_sequence_id = import_feature("inputs_segmentation")
    if not encoder_sequence_id:
      encoder_sequence_id = mtf.to_int32(mtf.not_equal(inputs, 0))
    decoder_sequence_id = import_feature("targets_segmentation")
    if decoder_sequence_id is None:
      decoder_sequence_id = mtf.to_int32(mtf.not_equal(targets, 0))
    model = self.model()
    logits, loss = model.call_simple(
        inputs=inputs,
        targets=targets,
        compute_loss=True,
        mode=hparams.mode,
        variable_dtype=self.variable_dtype,
        encoder_sequence_id=encoder_sequence_id,
        decoder_sequence_id=decoder_sequence_id)
    return logits, loss

  def sample(self, features, mesh):
    hparams = self._hparams
    model = self.model()
    inputs = self._import_feature(features, mesh, "inputs")
    return model.decode(
        inputs,
        self.variable_dtype,
        beam_size=hparams.beam_size,
        alpha=hparams.alpha,
        temperature=hparams.sampling_temp if hparams.beam_size == 1 else 0,
        decode_length_multiplier=hparams.decode_length_multiplier,
        decode_length_constant=hparams.decode_length_constant)


def default_layer_stack(hparams):
  return transformer.LayerStack(
      [transformer_layers.SelfAttention(
          num_heads=hparams.num_heads,
          key_value_size=hparams.d_kv,
          dropout_rate=hparams.attention_dropout),
       transformer_layers.DenseReluDense(
           hidden_size=hparams.d_ff,
           dropout_rate=hparams.relu_dropout),
      ] * hparams.num_hidden_layers,
      dropout_rate=hparams.layer_prepostprocess_dropout,
      norm_epsilon=hparams.norm_epsilon)


def default_layer_stack_with_encoder_attention(hparams):
  return transformer.LayerStack(
      [transformer_layers.SelfAttention(
          num_heads=hparams.num_heads,
          key_value_size=hparams.d_kv,
          dropout_rate=hparams.attention_dropout),
       transformer_layers.EncDecAttention(
           num_heads=hparams.num_heads,
           key_value_size=hparams.d_kv,
           dropout_rate=hparams.attention_dropout),
       transformer_layers.DenseReluDense(
           hidden_size=hparams.d_ff,
           dropout_rate=hparams.relu_dropout),
      ] * hparams.num_hidden_layers,
      dropout_rate=hparams.layer_prepostprocess_dropout,
      norm_epsilon=hparams.norm_epsilon)


def mtf_transformer2_base():
  """Set of hyperparameters."""
  hparams = common_hparams.basic_params1()

  hparams.add_hparam("d_model", 1024)
  hparams.batch_size = 4
  hparams.max_length = 1024
  hparams.label_smoothing = 0.0
  # a small positive value - this seems important for stability when training
  # with bfloat16 activations.
  hparams.add_hparam("z_loss", 1e-4)

  # These hyperparameters are used in default_layer_stack()
  # They may not be respected if hparams uses a differet layer stack function.
  hparams.num_hidden_layers = 6
  hparams.add_hparam("d_ff", 2048)
  hparams.add_hparam("d_kv", 128)
  hparams.add_hparam("attention_dropout", 0.0)
  hparams.add_hparam("relu_dropout", 0.0)
  hparams.layer_prepostprocess_dropout = 0.0

  # round up vocab sizes to be a multiple of this value
  hparams.vocab_divisor = 128

  hparams.optimizer = "Adafactor"
  hparams.learning_rate_schedule = "rsqrt_decay*linear_decay"
  hparams.learning_rate_warmup_steps = 10000
  hparams.add_hparam("master_dtype", "bfloat16")
  hparams.add_hparam("slice_dtype", "float32")
  hparams.activation_dtype = "bfloat16"

  # 8-way model-parallelism
  hparams.add_hparam("mesh_shape", "model:8")
  hparams.add_hparam("layout", "batch:batch;vocab:model;d_ff:model;heads:model")

  # If nonzero, we split the batch across two tensor-dimensions named
  # "outer_batch" and "inner_batch", allowing for splitting across two mesh
  # dimensions.  This is necessary for hierarchical mixture of experts.
  # The two tensor dimensions have sizes hparams.outer_batch_size and
  # hparams.batch_size // hparams.outer_batch_size.
  hparams.add_hparam("outer_batch_size", 0)

  hparams.shared_embedding_and_softmax_weights = False
  # length for training or decoding - defaults to max_length
  hparams.add_hparam("length", 0)

  # These parameters make Transformer model compatible with mtf
  # Do not override these.
  hparams.no_data_parallelism = True
  hparams.use_fixed_batch_size = True
  hparams.add_hparam("mtf_mode", True)
  hparams.clip_grad_norm = 0.  # i.e. no gradient clipping
  hparams.modality = {
      "inputs": modalities.IdentitySymbolModality,
      "targets": modalities.IdentitySymbolModality,
  }
  return hparams


@registry.register_hparams
def mtf_unitransformer_base():
  hparams = mtf_transformer2_base()
  hparams.add_hparam("autoregressive", True)
  hparams.layer_stack = default_layer_stack
  return hparams


@registry.register_hparams
def mtf_bitransformer_base():
  """Machine translation base configuration."""
  hparams = mtf_transformer2_base()
  hparams.max_length = 256
  hparams.shared_embedding = True
  hparams.encoder_layer_stack = default_layer_stack
  hparams.decoder_layer_stack = default_layer_stack_with_encoder_attention
  # Parameters for computing the maximum decode length in beam search.
  # Maximum decode length is:
  #    min(max_length,
  #        decode_length_multiplier * input_length + decode_length_constant)
  hparams.add_hparam("decode_length_multiplier", 1.5)
  hparams.add_hparam("decode_length_constant", 10.0)
  return hparams


@registry.register_hparams
def mtf_unitransformer_tiny():
  hparams = mtf_unitransformer_base()
  hparams.batch_size = 2
  hparams.mesh_shape = ""
  hparams.d_model = 128
  hparams.num_hidden_layers = 2
  hparams.num_heads = 4
  hparams.d_ff = 512
  return hparams


@registry.register_hparams
def mtf_bitransformer_tiny():
  hparams = mtf_bitransformer_base()
  hparams.batch_size = 2
  hparams.mesh_shape = ""
  hparams.d_model = 128
  hparams.num_hidden_layers = 2
  hparams.num_heads = 4
  hparams.d_ff = 512
  return hparams


@registry.register_hparams
def mtf_unitransformer_all_layers_tiny():
  """Test out all the layers on local CPU."""
  hparams = mtf_unitransformer_tiny()
  hparams.layer_stack = transformer.LayerStack(
      [transformer_layers.SelfAttention(num_heads=4),
       transformer_layers.LocalSelfAttention(num_heads=4),
       moe.MoE1D(num_experts=4, hidden_size=512),
       moe.MoE2D(expert_x=4, expert_y=4, hidden_size=512),
       transformer_layers.DenseReluDense(hidden_size=512)])
  return hparams


@registry.register_hparams
def mtr_lm_dense(sz):
  """Series of architectures for language modeling.

  We assume infinite training data, so no dropout necessary.

  You can use languagemodel_wiki_noref_v32k_l1k.
  (1 epoch = ~46000 steps).
  TODO(noam): find a large enough dataset for these experiments.

  Args:
    sz: an integer

  Returns:
    a hparams
  """
  n = 2 ** sz
  hparams = mtf_unitransformer_base()
  hparams.d_model = 1024
  hparams.max_length = 1024
  hparams.batch_size = 128
  # Parameters for my_layer_stack()
  hparams.num_hidden_layers = 6
  hparams.d_ff = 8192 * n
  hparams.d_kv = 256
  hparams.num_heads = 8 * n
  hparams.learning_rate_decay_steps = 65536
  hparams.layout = "batch:batch;vocab:model;d_ff:model;heads:model"
  hparams.mesh_shape = "batch:32"
  return hparams


@registry.register_hparams
def mtr_lm_dense_0():
  return mtr_lm_dense(0)


@registry.register_hparams
def mtr_lm_dense_1():
  return mtr_lm_dense(1)


@registry.register_hparams
def mtr_lm_dense_2():
  hparams = mtr_lm_dense(2)
  hparams.mesh_shape = "model:4;batch:8"
  return hparams


@registry.register_hparams
def mtr_lm_dense_3():
  hparams = mtr_lm_dense(3)
  hparams.mesh_shape = "model:4;batch:8"
  return hparams


@registry.register_hparams
def mtr_lm_v1():
  """Model incorporating mixture-of-experts, local and global attention.

  ~6B parameters

  32 experts in 3 hierarchichal moe layers.

  Returns:
    a hparams
  """
  hparams = mtr_lm_dense(0)
  local_att = transformer_layers.LocalSelfAttention(
      num_heads=4, key_value_size=128)
  att = transformer_layers.SelfAttention(num_heads=4, key_value_size=128)
  drd = transformer_layers.DenseReluDense(hidden_size=2048)
  hmoe = moe.MoE2D(expert_x=8, expert_y=4, hidden_size=32768)
  hparams.layer_stack = transformer.LayerStack(
      ([local_att, local_att, drd,
        att, drd, local_att, local_att, hmoe] * 4)[:-1])
  hparams.mesh_shape = "b0:4;b1:8"
  hparams.layout = "outer_batch:b0;inner_batch:b1,expert_x:b1,expert_y:b0"
  hparams.outer_batch_size = 4
  return hparams


@registry.register_hparams
def mtr_tr_dense(sz):
  """Series of machine translation models.

  All models are trained on sequences of 256 tokens.

  You can use the dataset translate_enfr_wmt32k_packed.
  154000 steps = 3 epochs.

  Args:
    sz: an integer

  Returns:
    a hparams
  """
  n = 2 ** sz
  hparams = mtf_bitransformer_base()
  hparams.d_model = 1024
  hparams.max_length = 256
  hparams.batch_size = 128
  # Parameters for my_layer_stack()
  hparams.num_hidden_layers = 6
  hparams.d_ff = int(4096 * n)
  hparams.d_kv = 128
  hparams.num_heads = int(8 * n)
  # one epoch for translate_enfr_wmt32k_packed = 51400 steps
  hparams.learning_rate_decay_steps = 51400
  hparams.layout = "batch:batch;vocab:model;d_ff:model;heads:model"
  hparams.mesh_shape = "model:4;batch:8"
  hparams.label_smoothing = 0.1
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.attention_dropout = 0.1
  hparams.relu_dropout = 0.1
  return hparams


@registry.register_hparams
def mtr_tr_dense_0():
  return mtr_tr_dense(0)


@registry.register_hparams
def mtr_tr_dense_1():
  return mtr_tr_dense(1)


@registry.register_hparams
def mtr_tr_dense_2():
  return mtr_tr_dense(2)


@registry.register_hparams
def mtr_tr_dense_3():
  return mtr_tr_dense(3)


@registry.register_hparams
def mtr_tr_dense_0_short():
  hparams = mtr_tr_dense(0)
  hparams.num_hidden_layers = 3
  return hparams
