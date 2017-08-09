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

"""Utilities for trainer binary."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys

# Dependency imports

from tensor2tensor.data_generators import all_problems  # pylint: disable=unused-import
from tensor2tensor.data_generators import problem_hparams
from tensor2tensor.models import models  # pylint: disable=unused-import
from tensor2tensor.utils import data_reader
from tensor2tensor.utils import decoding
from tensor2tensor.utils import devices
from tensor2tensor.utils import input_fn_builder
from tensor2tensor.utils import metrics
from tensor2tensor.utils import model_builder
from tensor2tensor.utils import registry

import tensorflow as tf
from tensorflow.contrib.learn.python.learn import learn_runner
from tensorflow.python import debug

flags = tf.flags
FLAGS = flags.FLAGS

flags.DEFINE_bool("registry_help", False,
                  "If True, logs the contents of the registry and exits.")
flags.DEFINE_bool("tfdbg", False,
                  "If True, use the TF debugger CLI on train/eval.")
flags.DEFINE_string("output_dir", "", "Base output directory for run.")
flags.DEFINE_string("model", "", "Which model to use.")
flags.DEFINE_string("hparams_set", "", "Which parameters to use.")
flags.DEFINE_string("hparams_range", "", "Parameters range.")
flags.DEFINE_string(
    "hparams", "",
    """A comma-separated list of `name=value` hyperparameter values. This flag
    is used to override hyperparameter settings either when manually selecting
    hyperparameters or when using Vizier. If a hyperparameter setting is
    specified by this flag then it must be a valid hyperparameter name for the
    model.""")
flags.DEFINE_string("problems", "", "Dash separated list of problems to "
                    "solve.")
flags.DEFINE_string("data_dir", "/tmp/data", "Directory with training data.")
flags.DEFINE_integer("train_steps", 250000,
                     "The number of steps to run training for.")
flags.DEFINE_integer("eval_steps", 10, "Number of steps in evaluation.")
flags.DEFINE_bool("eval_print", False, "Print eval logits and predictions.")
flags.DEFINE_bool("eval_run_autoregressive", False,
                  "Run eval autoregressively where we condition on previous"
                  "generated output instead of the actual target.")
flags.DEFINE_integer("keep_checkpoint_max", 20,
                     "How many recent checkpoints to keep.")
flags.DEFINE_bool("experimental_optimize_placement", False,
                  "Optimize ops placement with experimental session options.")

# Distributed training flags
flags.DEFINE_string("master", "", "Address of TensorFlow master.")
flags.DEFINE_string("schedule", "local_run",
                    "Method of tf.contrib.learn.Experiment to run.")
flags.DEFINE_integer("local_eval_frequency", 2000,
                     "Run evaluation every this steps during local training.")
flags.DEFINE_bool("locally_shard_to_cpu", False,
                  "Use CPU as a sharding device running locally. This allows "
                  "to test sharded model construction on a machine with 1 GPU.")
flags.DEFINE_bool("daisy_chain_variables", True,
                  "copy variables around in a daisy chain")
flags.DEFINE_bool("sync", False, "Sync compute on PS.")
flags.DEFINE_string("worker_job", "/job:worker", "name of worker job")
flags.DEFINE_integer("worker_gpu", 1, "How many GPUs to use.")
flags.DEFINE_integer("worker_replicas", 1, "How many workers to use.")
flags.DEFINE_integer("worker_id", 0, "Which worker task are we.")
flags.DEFINE_float("worker_gpu_memory_fraction", 1.,
                   "Fraction of GPU memory to allocate.")
flags.DEFINE_integer("ps_gpu", 0, "How many GPUs to use per ps.")
flags.DEFINE_string("gpu_order", "", "Optional order for daisy-chaining gpus."
                    " e.g. \"1 3 2 4\"")
flags.DEFINE_string("ps_job", "/job:ps", "name of ps job")
flags.DEFINE_integer("ps_replicas", 0, "How many ps replicas.")

# Decode flags
# Set one of {decode_from_dataset, decode_interactive, decode_from_file} to
# decode.
flags.DEFINE_bool("decode_from_dataset", False, "Decode from dataset on disk.")
flags.DEFINE_bool("decode_use_last_position_only", False,
                  "In inference, use last position only for speedup.")
flags.DEFINE_bool("decode_interactive", False,
                  "Interactive local inference mode.")
flags.DEFINE_bool("decode_save_images", False, "Save inference input images.")
flags.DEFINE_string("decode_from_file", None, "Path to decode file")
flags.DEFINE_string("decode_to_file", None, "Path to inference output file")
flags.DEFINE_integer("decode_shards", 1, "How many shards to decode.")
flags.DEFINE_integer("decode_problem_id", 0, "Which problem to decode.")
flags.DEFINE_integer("decode_extra_length", 50, "Added decode length.")
flags.DEFINE_integer("decode_batch_size", 32, "Batch size for decoding. "
                     "The decodes will be written to <filename>.decodes in"
                     "format result\tinput")
flags.DEFINE_integer("decode_beam_size", 4, "The beam size for beam decoding")
flags.DEFINE_float("decode_alpha", 0.6, "Alpha for length penalty")
flags.DEFINE_bool("decode_return_beams", False,
                  "Whether to return 1 (False) or all (True) beams. The \n "
                  "output file will have the format "
                  "<beam1>\t<beam2>..\t<input>")
flags.DEFINE_integer("decode_max_input_size", -1,
                     "Maximum number of ids in input. Or <= 0 for no max.")
flags.DEFINE_bool("identity_output", False, "To print the output as identity")
flags.DEFINE_integer("decode_num_samples", -1,
                     "Number of samples to decode. Currently used in"
                     "decode_from_dataset. Use -1 for all.")


def make_experiment_fn(data_dir, model_name, train_steps, eval_steps):
  """Returns experiment_fn for learn_runner. Wraps create_experiment."""

  def experiment_fn(output_dir):
    return create_experiment(
        output_dir=output_dir,
        data_dir=data_dir,
        model_name=model_name,
        train_steps=train_steps,
        eval_steps=eval_steps)

  return experiment_fn


def create_experiment(output_dir, data_dir, model_name, train_steps,
                      eval_steps):
  """Create Experiment."""
  hparams = create_hparams(FLAGS.hparams_set, data_dir)
  estimator, input_fns = create_experiment_components(
      hparams=hparams,
      output_dir=output_dir,
      data_dir=data_dir,
      model_name=model_name)
  eval_metrics = metrics.create_evaluation_metrics(
      zip(FLAGS.problems.split("-"), hparams.problem_instances), hparams)
  if (hasattr(FLAGS, "autotune") and FLAGS.autotune and
      FLAGS.objective not in eval_metrics):
    raise ValueError("Tuning objective %s not among evaluation metrics %s" %
                     (FLAGS.objective, eval_metrics.keys()))
  train_monitors = []
  eval_hooks = []
  if FLAGS.tfdbg:
    hook = debug.LocalCLIDebugHook()
    train_monitors.append(hook)
    eval_hooks.append(hook)
  return tf.contrib.learn.Experiment(
      estimator=estimator,
      train_input_fn=input_fns[tf.contrib.learn.ModeKeys.TRAIN],
      eval_input_fn=input_fns[tf.contrib.learn.ModeKeys.EVAL],
      eval_metrics=eval_metrics,
      train_steps=train_steps,
      eval_steps=eval_steps,
      min_eval_frequency=FLAGS.local_eval_frequency,
      train_monitors=train_monitors,
      eval_hooks=eval_hooks)


def create_experiment_components(hparams, output_dir, data_dir, model_name):
  """Constructs and returns Estimator and train/eval input functions."""
  tf.logging.info("Creating experiment, storing model files in %s", output_dir)

  num_datashards = devices.data_parallelism().n
  train_input_fn = input_fn_builder.build_input_fn(
      mode=tf.contrib.learn.ModeKeys.TRAIN,
      hparams=hparams,
      data_file_patterns=get_data_filepatterns(data_dir,
                                               tf.contrib.learn.ModeKeys.TRAIN),
      num_datashards=num_datashards,
      worker_replicas=FLAGS.worker_replicas,
      worker_id=FLAGS.worker_id)

  eval_input_fn = input_fn_builder.build_input_fn(
      mode=tf.contrib.learn.ModeKeys.EVAL,
      hparams=hparams,
      data_file_patterns=get_data_filepatterns(data_dir,
                                               tf.contrib.learn.ModeKeys.EVAL),
      num_datashards=num_datashards,
      worker_replicas=FLAGS.worker_replicas,
      worker_id=FLAGS.worker_id)
  estimator = tf.contrib.learn.Estimator(
      model_fn=model_builder.build_model_fn(model_name, hparams=hparams),
      model_dir=output_dir,
      config=tf.contrib.learn.RunConfig(
          master=FLAGS.master,
          model_dir=output_dir,
          gpu_memory_fraction=FLAGS.worker_gpu_memory_fraction,
          session_config=session_config(),
          keep_checkpoint_max=FLAGS.keep_checkpoint_max))
  # Store the hparams in the estimator as well
  estimator.hparams = hparams
  return estimator, {
      tf.contrib.learn.ModeKeys.TRAIN: train_input_fn,
      tf.contrib.learn.ModeKeys.EVAL: eval_input_fn
  }


def log_registry():
  if FLAGS.registry_help:
    tf.logging.info(registry.help_string())
    sys.exit(0)


def add_problem_hparams(hparams, problems):
  """Add problem hparams for the problems."""
  hparams.problems = []
  hparams.problem_instances = []
  for problem_name in problems.split("-"):
    try:
      problem = registry.problem(problem_name)
    except ValueError:
      problem = None

    if problem is None:
      p_hparams = problem_hparams.problem_hparams(problem_name, hparams)
    else:
      p_hparams = problem.internal_hparams(hparams)

    hparams.problem_instances.append(problem)
    hparams.problems.append(p_hparams)

  return hparams


def create_hparams(params_id, data_dir):
  """Returns hyperparameters, including any flag value overrides.

  If the hparams FLAG is set, then it will use any values specified in
  hparams to override any individually-set hyperparameter. This logic
  allows tuners to override hyperparameter settings to find optimal values.

  Args:
    params_id: which set of parameters to choose (must be in _PARAMS above).
    data_dir: the directory containing the training data.

  Returns:
    The hyperparameters as a tf.contrib.training.HParams object.
  """
  hparams = registry.hparams(params_id)()
  hparams.add_hparam("data_dir", data_dir)
  # Command line flags override any of the preceding hyperparameter values.
  if FLAGS.hparams:
    hparams = hparams.parse(FLAGS.hparams)

  return add_problem_hparams(hparams, FLAGS.problems)


def run(data_dir, model, output_dir, train_steps, eval_steps, schedule):
  """Runs an Estimator locally or distributed.

  This function chooses one of two paths to execute:

  1. Running locally if schedule=="local_run".
  3. Distributed training/evaluation otherwise.

  Args:
    data_dir: The directory the data can be found in.
    model: The name of the model to use.
    output_dir: The directory to store outputs in.
    train_steps: The number of steps to run training for.
    eval_steps: The number of steps to run evaluation for.
    schedule: (str) The schedule to run. The value here must
      be the name of one of Experiment's methods.
  """
  exp_fn = make_experiment_fn(
      data_dir=data_dir,
      model_name=model,
      train_steps=train_steps,
      eval_steps=eval_steps)

  if schedule == "local_run":
    # Run the local demo.
    exp = exp_fn(output_dir)
    if exp.train_steps > 0 or exp.eval_steps > 0:
      tf.logging.info("Performing local training and evaluation.")
      exp.train_and_evaluate()
    decode(exp.estimator)
  else:
    # Perform distributed training/evaluation.
    learn_runner.run(
        experiment_fn=exp_fn, schedule=schedule, output_dir=output_dir)


def validate_flags():
  if not FLAGS.model:
    raise ValueError("Must specify a model with --model.")
  if not FLAGS.problems:
    raise ValueError("Must specify a set of problems with --problems.")
  if not (FLAGS.hparams_set or FLAGS.hparams_range):
    raise ValueError("Must specify either --hparams_set or --hparams_range.")
  if not FLAGS.schedule:
    raise ValueError("Must specify --schedule.")
  if not FLAGS.output_dir:
    FLAGS.output_dir = "/tmp/tensor2tensor"
    tf.logging.warning("It is strongly recommended to specify --output_dir. "
                       "Using default output_dir=%s.", FLAGS.output_dir)


def session_config():
  """The TensorFlow Session config to use."""
  graph_options = tf.GraphOptions(optimizer_options=tf.OptimizerOptions(
      opt_level=tf.OptimizerOptions.L1, do_function_inlining=False))

  if FLAGS.experimental_optimize_placement:
    rewrite_options = tf.RewriterConfig(optimize_tensor_layout=True)
    rewrite_options.optimizers.append("pruning")
    rewrite_options.optimizers.append("constfold")
    rewrite_options.optimizers.append("layout")
    graph_options = tf.GraphOptions(
        rewrite_options=rewrite_options, infer_shapes=True)

  gpu_options = tf.GPUOptions(
      per_process_gpu_memory_fraction=FLAGS.worker_gpu_memory_fraction)

  config = tf.ConfigProto(
      allow_soft_placement=True,
      graph_options=graph_options,
      gpu_options=gpu_options)
  return config


def get_data_filepatterns(data_dir, mode):
  return data_reader.get_data_filepatterns(FLAGS.problems, data_dir, mode)


def decode(estimator):
  if FLAGS.decode_interactive:
    decoding.decode_interactively(estimator)
  elif FLAGS.decode_from_file is not None:
    decoding.decode_from_file(estimator, FLAGS.decode_from_file)
  elif FLAGS.decode_from_dataset:
    decoding.decode_from_dataset(estimator)
