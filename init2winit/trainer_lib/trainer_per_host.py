# coding=utf-8
# Copyright 2022 The init2winit Authors.
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

"""Standard trainer for the init2winit project."""
import functools
import itertools
import os
import time

from absl import logging
from flax import jax_utils
from init2winit import callbacks
from init2winit import checkpoint
from init2winit import schedules
from init2winit import utils
from init2winit.dataset_lib import data_utils
from init2winit.init_lib import init_utils
from init2winit.model_lib import model_utils
from init2winit.optimizer_lib import gradient_accumulator
from init2winit.optimizer_lib import optimizers
from init2winit.trainer_lib import trainer_utils
from init2winit.training_metrics_grabber import make_training_metrics
import jax
from jax import lax
from jax.experimental import multihost_utils
import jax.numpy as jnp
import numpy as np
import optax

_GRAD_CLIP_EPS = 1e-6


def gather_multihost_report(report, current_expert):
  """Gathers eval metrics from all hosts.

  Args:
    report: report generated from `eval_metrics`.
    current_expert: expert to use for primary metrics.

  Returns:
    report: report gathered from all hosts and arranged as a dict of scalars.
  """
  all_reports = multihost_utils.process_allgather(report)

  for metric in report.keys():
    metric_reports = {
        f'{metric}-{i}': x.item() for i, x in enumerate(all_reports[metric])
    }
    all_reports.update(metric_reports)
    all_reports[metric] = all_reports[f'{metric}-{current_expert}']

  return all_reports


def add_expert_probs(report, expert_weights):
  probs = expert_weights.sum(axis=0) / expert_weights.sum()
  probs = {f'expert_prob-{i}': p for i, p in enumerate(probs)}
  report.update(probs)
  return report


def _sync_batchnorm_stats(batch_stats):
  return jax.lax.pmean(
      batch_stats,
      axis_name='batch',
      axis_index_groups=_get_axis_index_groups())


def _maybe_sync_batchnorm_stats(batch_stats):
  if batch_stats:
    batch_stats = jax.pmap(
        _sync_batchnorm_stats, axis_name='batch')(
            batch_stats)
  return batch_stats


def _get_axis_index_groups():
  """Generate axis_index_groups for collective operations.

  Ensures that each host independently aggregates over its local devices.

  Returns:
    axis_index_groups

  """
  # Example: suppose num_d=8, num_p=4
  # - ``groups`` will be [[0,...,7],...,[24,...,31]].
  # - The first 8 devices (i.e., associated with process 0) will aggregate for
  # process 0.
  # - The second 8 devices (i.e., associated with process 1) will aggregate for
  # process 1.
  # - etc.
  num_d, num_p = jax.local_device_count(), jax.process_count()
  return [list(range(i * num_d, i * num_d + num_d)) for i in range(num_p)]


def evaluate_batch(model, params, batch_stats, batch):
  """Evaluates metrics on the given batch.

  Merge per host, not across all hosts.

  NOTE(dsuo): this does not work for models that do not use the default
  _evaluate_batch function in `base_model.py`.

  We use the CLU metrics library to evaluate the metrics, and we require that
  each metric_fn in metrics_bundle has the API:
    metric_fn(logits, targets, weights), including the argument names.

  Args:
    model: init2winit model.
    params: A dict of trainable model parameters. Passed as {'params': params}
      into flax_module.apply().
    batch_stats: A dict of non-trainable model state. Passed as
      {'batch_stats': batch_stats} into flax_module.apply().
    batch: A dictionary with keys 'inputs', 'targets', 'weights'.

  Returns:
    A dictionary with the same keys as metrics, but mapping to the summed metric
    across the sharded batch_dim.
  """
  variables = {'params': params, 'batch_stats': batch_stats}
  logits = model.flax_module.apply(
      variables, batch['inputs'], mutable=False, train=False)
  targets = batch['targets']

  if model.dataset_meta_data['apply_one_hot_in_loss']:
    targets = jax.nn.one_hot(batch['targets'], logits.shape[-1])

  # map the dict values (which are functions) to function(targets, logits)
  weights = batch.get('weights')  # Weights might not be defined.
  eval_batch_size = targets.shape[0]
  if weights is None:
    weights = jnp.ones(eval_batch_size)

  # We don't use CLU's `mask` argument here, we handle it ourselves through
  # `weights`.
  return jax.lax.all_gather(
      model.metrics_bundle._from_model_output(  # pylint: disable=protected-access
          logits=logits, targets=targets, weights=weights),
      axis_name='batch',
      axis_index_groups=_get_axis_index_groups()).reduce()


def evaluate(params, batch_stats, batch_iter, evaluate_batch_pmapped):
  """Compute aggregated metrics on the given data iterator.

  WARNING: The caller is responsible for synchronizing the batch norm statistics
  before calling this function!

  Assumed API of evaluate_batch_pmapped:
  metrics = evaluate_batch_pmapped(params, batch_stats, batch)
  where batch is yielded by the batch iterator, and metrics is a dictionary
  mapping metric name to a vector of per example measurements. The metrics are
  merged using the CLU metrics logic for that metric type. See
  classification_metrics.py for a definition of evaluate_batch.

  Args:
    params: a dict of trainable model parameters. Passed as {'params': params}
      into flax_module.apply().
    batch_stats: a dict of non-trainable model state. Passed as
      {'batch_stats': batch_stats} into flax_module.apply().
    batch_iter: Generator which yields batches. Must support the API
      for b in batch_iter:
    evaluate_batch_pmapped: A function with API
       evaluate_batch_pmapped(params, batch_stats, batch). Returns a dictionary
       mapping keys to the metric values across the sharded batch.

  Returns:
    A dictionary of aggregated metrics. The keys will match the keys returned by
    evaluate_batch_pmapped.
  """
  metrics = None
  for batch in batch_iter:
    batch = data_utils.shard(batch)
    # Returns a clu.metrics.Collection object. We assume that
    # `evaluate_batch_pmpapped` calls CLU's `gather_from_model_outputs`,
    # which includes an `all_gather` to replicate the values on all devices.
    # We need to `unreplicate` before merging the results across batches to
    # accommodate CollectingMetric, which concatenates the values across the
    # leading dimension, so we need to remove the leading shard dimension first.
    computed_metrics = evaluate_batch_pmapped(
        params=params, batch_stats=batch_stats, batch=batch).unreplicate()
    if metrics is None:
      metrics = computed_metrics
    else:
      # `merge` aggregates the metrics across batches.
      metrics = metrics.merge(computed_metrics)

  # For data splits with no data (e.g. Imagenet no test set) no values
  # will appear for that split.
  if metrics is not None:
    # `compute` aggregates the metrics across batches into a single value.
    metrics = metrics.compute()
    for key, val in metrics.items():
      if np.isnan(val):
        raise utils.TrainingDivergedError('NaN detected in {}'.format(key))
  return metrics


def _inject_learning_rate(optimizer_state, lr):
  """Inject the given LR into any optimizer state that will accept it."""
  # The optimizer state should always be an InjectHyperparamsState, and we
  # inject the learning rate into all states that will accept it. We need to do
  # this to allow arbitrary (non-jittable) LR schedules.
  if isinstance(optimizer_state, optax.InjectHyperparamsState):
    if 'learning_rate' in optimizer_state.hyperparams:
      optimizer_state.hyperparams['learning_rate'] = lr
  elif isinstance(
      optimizer_state, gradient_accumulator.GradientAccumulatorState):
    _inject_learning_rate(optimizer_state.base_state, lr)
  else:
    raise ValueError(
        'Unsupported optimizer_state type given when trying to inject the '
        'learning rate:\n\n{}.'.format(optimizer_state))


def update(
    optimizer_state,
    params,
    batch_stats,
    metrics_state,
    batch,
    step,
    lr,
    rng,
    local_device_index,
    training_cost,
    grad_clip,
    optimizer_update_fn,
    metrics_update_fn):
  """Single step of the training loop.

  Args:
    optimizer_state: the optax optimizer state.
    params: a dict of trainable model parameters. Passed into training_cost(...)
      which then passes into flax_module.apply() as {'params': params} as part
      of the variables dict.
    batch_stats: a dict of non-trainable model state. Passed into
      training_cost(...) which then passes into flax_module.apply() as
      {'batch_stats': batch_stats} as part of the variables dict.
    metrics_state: a pytree of training metrics state.
    batch: the per-device batch of data to process.
    step: the current global step of this update. Used to fold in to `rng` to
      produce a unique per-device, per-step RNG.
    lr: the floating point learning rate for this step.
    rng: the RNG used for calling the model. `step` and `local_device_index`
      will be folded into this to produce a unique per-device, per-step RNG.
    local_device_index: an integer that is unique to this device amongst all
      devices on this host, usually in the range [0, jax.local_device_count()].
      It is folded in to `rng` to produce a unique per-device, per-step RNG.
    training_cost: a function used to calculate the training objective that will
      be differentiated to generate updates. Takes
      (`params`, `batch`, `batch_stats`, `dropout_rng`) as inputs.
    grad_clip: Clip the l2 norm of the gradient at the specified value. For
      minibatches with gradient norm ||g||_2 > grad_clip, we rescale g to the
      value g / ||g||_2 * grad_clip. If None, then no clipping will be applied.
    optimizer_update_fn: the optimizer update function.
    metrics_update_fn: the training metrics update function.

  Returns:
    A tuple of the new optimizer, the new batch stats, the scalar training cost,
    the new training metrics state, and the gradient norm.
  """
  # `jax.random.split` is very slow outside the train step, so instead we do a
  # `jax.random.fold_in` here.
  rng = jax.random.fold_in(rng, step)
  rng = jax.random.fold_in(rng, local_device_index)

  _inject_learning_rate(optimizer_state, lr)

  def opt_cost(params):
    return training_cost(
        params, batch=batch, batch_stats=batch_stats, dropout_rng=rng)

  grad_fn = jax.value_and_grad(opt_cost, has_aux=True)
  (cost_value, new_batch_stats), grad = grad_fn(params)
  new_batch_stats = new_batch_stats.get('batch_stats', None)

  cost_value, grad = lax.pmean((cost_value, grad),
                               axis_name='batch',
                               axis_index_groups=_get_axis_index_groups())

  grad_norm = jnp.sqrt(model_utils.l2_regularization(grad, 0))
  # TODO(znado): move to inside optax gradient clipping.
  if grad_clip:
    scaled_grad = jax.tree_map(
        lambda x: x / (grad_norm + _GRAD_CLIP_EPS) * grad_clip, grad)
    grad = jax.lax.cond(grad_norm > grad_clip, lambda _: scaled_grad,
                        lambda _: grad, None)

  if 'train_loss' in optimizer_state.hyperparams:
    # Inject train_loss into optimizer state.
    optimizer_state.hyperparams['train_loss'] = cost_value

  model_updates, new_optimizer_state = optimizer_update_fn(
      grad,
      optimizer_state,
      params=params,
      batch=batch,
      batch_stats=new_batch_stats)
  new_params = optax.apply_updates(params, model_updates)

  new_metrics_state = None
  if metrics_state is not None:
    new_metrics_state = metrics_update_fn(metrics_state, grad, params,
                                          new_params)

  return (new_optimizer_state, new_params, new_batch_stats, cost_value,
          new_metrics_state, grad_norm)


def _merge_and_apply_prefix(d1, d2, prefix):
  d1 = d1.copy()
  for key in d2:
    d1[prefix+key] = d2[key]
  return d1


@utils.timed
def eval_metrics(params, batch_stats, dataset, eval_num_batches,
                 eval_train_num_batches, evaluate_batch_pmapped):
  """Evaluates the given network on the train, validation, and test sets.

  WARNING: we assume that `batch_stats` has already been synchronized across
  devices before being passed to this function! See
  `trainer_utils.maybe_sync_batchnorm_stats`.

  The metric names will be of the form split/measurement for split in the set
  {train, valid, test} and measurement in the set {loss, error_rate}.

  Args:
    params: a dict of trainable model parameters. Passed as {'params': params}
      into flax_module.apply().
    batch_stats: a dict of non-trainable model state. Passed as
      {'batch_stats': batch_stats} into flax_module.apply().
    dataset: Dataset returned from datasets.get_dataset. train, validation, and
      test sets.
    eval_num_batches: (int) The batch size used for evaluating on validation,
      and test sets. Set to None to evaluate on the whole test set.
    eval_train_num_batches: (int) The batch size used for evaluating on train
      set. Set to None to evaluate on the whole training set.
    evaluate_batch_pmapped: Computes the metrics on a sharded batch.

  Returns:
    A dictionary of all computed metrics.
  """
  train_iter = dataset.eval_train_epoch(eval_train_num_batches)
  valid_iter = dataset.valid_epoch(eval_num_batches)
  test_iter = dataset.test_epoch(eval_num_batches)

  metrics = {}
  for split_iter, split_name in zip([train_iter, valid_iter, test_iter],
                                    ['train', 'valid', 'test']):
    split_metrics = evaluate(params, batch_stats, split_iter,
                             evaluate_batch_pmapped)
    # Metrics are None if the dataset doesn't have that split
    if split_metrics is not None:
      metrics = _merge_and_apply_prefix(
          metrics, split_metrics, (split_name + '/'))
  return metrics


def train(train_dir,
          model,
          dataset_builder,
          initializer,
          num_train_steps,
          hps,
          rng,
          eval_batch_size,
          eval_num_batches,
          eval_train_num_batches,
          eval_frequency,
          checkpoint_steps,
          early_stopping_target_name=None,
          early_stopping_target_value=None,
          early_stopping_mode=None,
          eval_steps=None,
          metrics_logger=None,
          init_logger=None,
          training_metrics_config=None,
          callback_configs=None,
          external_checkpoint_path=None):
  """Main training loop.

  Trains the given network on the specified dataset for the given number of
  epochs. Saves the training curve in train_dir/r=3/results.tsv.

  Args:
    train_dir: (str) Path of the training directory.
    model: (BaseModel) Model object to be trained.
    dataset_builder: dataset builder returned by datasets.get_dataset.
    initializer: Must have API as defined in initializers.py
    num_train_steps: (int) Number of steps to train on.
    hps: (tf.HParams) Model, initialization and training hparams.
    rng: (jax.random.PRNGKey) Rng seed used in model initialization and data
      shuffling.
    eval_batch_size: the evaluation batch size. If None, use hps.batch_size.
    eval_num_batches: (int) The number of batches used for evaluating on
      validation and test sets. Set to None to evaluate on the whole train set.
    eval_train_num_batches: (int) The number of batches for evaluating on train.
      Set to None to evaluate on the whole training set.
    eval_frequency: (int) Evaluate every k steps.
    checkpoint_steps: List of integers indicating special steps to save
      checkpoints at. These checkpoints do not get used for preemption recovery.
    early_stopping_target_name: A string naming the metric to use to perform
       early stopping. If this metric reaches the value
      `early_stopping_target_value`, training will stop. Must include the
      dataset split (ex: validation/error_rate).
    early_stopping_target_value: A float indicating the value at which to stop
      training.
    early_stopping_mode: One of "above" or "below", indicates if we should stop
      when the metric is above or below the threshold value. Example: if
      "above", then training will stop when
      `report[early_stopping_target_name] >= early_stopping_target_value`.
    eval_steps: List of integers indicating which steps to perform evals. If
      provided, eval_frequency will be ignored. Performing an eval implies
      saving a checkpoint that will be used to resume training in the case of
      preemption.
    metrics_logger: Used to log all eval metrics during training. See
      utils.MetricLogger for API definition.
    init_logger: Used for black box initializers that have learning curves.
    training_metrics_config: Dict specifying the configuration of the
      training_metrics_grabber. Set to None to skip logging of advanced training
      metrics.
    callback_configs: List of configs specifying general callbacks to run
      during the eval phase. Empty list means no callbacks are run. See
      callbacks.py for details on what is expected in a config.
    external_checkpoint_path: (str) If this argument is set, we will load the
      optimizer_state, params, batch_stats, and training_metrics from the
      checkpoint at this location.

  Yields:
    metrics: A dictionary of all eval metrics from the given epoch.
  """
  # NOTE: the initialization RNG should *not* be per-host, as this will create
  # different sets of weights per host. However, all other RNGs should be
  # per-host.
  # TODO(znado,gilmer,gdahl): implement replicating the same initialization
  # across hosts.
  rng, init_rng = jax.random.split(rng)

  # NOTE(dsuo): fixing data_rng per host for running expert parallelism across
  # hosts.
  rng, data_rng = jax.random.split(rng)
  rng = jax.random.fold_in(rng, jax.process_index())

  # only used if checkpoints_steps is non-empty.
  checkpoint_dir = os.path.join(train_dir, 'checkpoints')

  if jax.process_index() == 0:
    logging.info('Let the training begin!')
    logging.info('Dataset input shape: %r', hps.input_shape)
    logging.info('Hyperparameters: %s', hps)

  if eval_batch_size is None:
    eval_batch_size = hps.batch_size
  if callback_configs is None:
    callback_configs = []

  # Maybe run the initializer.
  unreplicated_params, unreplicated_batch_stats = init_utils.initialize(
      model.flax_module,
      initializer,
      model.loss_fn,
      hps.input_shape,
      hps.output_shape,
      hps,
      init_rng,
      init_logger,
      model.get_fake_batch(hps))

  if jax.process_index() == 0:
    utils.log_pytree_shape_and_statistics(unreplicated_params)
    logging.info('train_size: %d,', hps.train_size)

  # Note that global_step refers to the number of gradients calculations, not
  # the number of model updates. This means when using gradient accumulation,
  # one must supply configs where the number of steps are in units of gradient
  # calculations, not model updates, and in post processing one must divide
  # global_step by grad_accum_step_multiplier to get the number of updates.
  #
  # If using gradient accumulation, stretch the learning rate schedule by the
  # number of gradient calculations per weight update.
  stretch_factor = 1
  if hps.get('total_accumulated_batch_size') is not None:
    stretch_factor = hps.total_accumulated_batch_size // hps.batch_size
  lr_fn = schedules.get_schedule_fn(
      hps.lr_hparams, num_train_steps, stretch_factor=stretch_factor)

  optimizer_init_fn, optimizer_update_fn = optimizers.get_optimizer(hps, model)
  unreplicated_optimizer_state = optimizer_init_fn(unreplicated_params)
  checkpointed_expert = unreplicated_optimizer_state.inner_state.current_expert

  (unreplicated_metrics_state, metrics_update_fn, metrics_summary_fn
   ) = None, None, None
  if training_metrics_config is not None:
    (metrics_init_fn, metrics_update_fn, metrics_summary_fn
     ) = make_training_metrics(**training_metrics_config)
    unreplicated_metrics_state = metrics_init_fn(unreplicated_params)

  (optimizer_state, params, batch_stats, metrics_state,
   global_step, sum_train_cost, preemption_count, is_restored
   ) = checkpoint.replicate_and_maybe_restore_checkpoint(
       unreplicated_optimizer_state,
       unreplicated_params,
       unreplicated_batch_stats,
       unreplicated_metrics_state,
       train_dir=train_dir,
       external_checkpoint_path=external_checkpoint_path)

  if is_restored:
    # TODO(dsuo): optimizer state is not checkpointed correctly, but will need
    # more thought how to properly checkpoint / integrate with init2winit.
    preemption_count += 1
    # Fold the restored step into the dataset RNG so that we will get a
    # different shuffle each time we restore, so that we do not repeat a
    # previous dataset ordering again after restoring. This is not the only
    # difference in shuffling each pre-emption, because we often times reshuffle
    # the input files each time in a non-deterministic manner.
    #
    # Note that if we are pre-empted more than once per epoch then we will
    # retrain more on the beginning of the training split, because each time we
    # restore we refill the shuffle buffer with the first `shuffle_buffer_size`
    # elements from the training split to continue training.
    #
    # Also note that for evaluating on the training split, because we are
    # reshuffling each time, we will get a new eval_train split each time we are
    # pre-empted.
    data_rng = jax.random.fold_in(data_rng, global_step)

  assert hps.batch_size % (jax.device_count()) == 0
  assert eval_batch_size % (jax.device_count()) == 0
  dataset = dataset_builder(
      data_rng,
      hps.batch_size,
      eval_batch_size=eval_batch_size,
      hps=hps,
  )

  update_fn = functools.partial(
      update,
      training_cost=model.training_cost,
      grad_clip=hps.get('grad_clip'),
      optimizer_update_fn=optimizer_update_fn,
      metrics_update_fn=metrics_update_fn)
  # in_axes = (
  #     optimizer_state = 0,
  #     params = 0,
  #     batch_stats = 0,
  #     metrics_state = 0,
  #     batch = 0,
  #     step = None,
  #     lr = None,
  #     rng = None,
  #     local_device_index = 0,
  #     training_cost,
  #     grad_clip,
  #     optimizer_update_fn,
  #     metrics_state_update_fn)
  update_pmapped = jax.pmap(
      update_fn,
      axis_name='batch',
      in_axes=(0, 0, 0, 0, 0, None, None, None, 0))
  evaluate_batch_pmapped = jax.pmap(
      functools.partial(evaluate_batch, model),
      axis_name='batch')
  start_time = time.time()
  start_step = global_step
  prev_eval_step = start_step
  def get_step_frequency(cur_step):
    return float(cur_step - start_step) / (time.time() - start_time)

  if jax.process_index() == 0:
    logging.info('Starting training!')

  # Numpy array of range(0, local_device_count) to send to each device to be
  # folded into the RNG inside each train step to get a unique per-device RNG.
  local_device_indices = np.arange(jax.local_device_count())

  # Start at the resumed step and continue until we have finished the number of
  # training steps. If building a dataset iterator using a tf.data.Dataset, in
  # the case of a batch size that does not evenly divide the training dataset
  # size, if using `ds.batch(..., drop_remainer=True)` on the training dataset
  # then the final batch in this iterator will be a partial batch. However, if
  # `drop_remainer=False`, then this iterator will always return batches of the
  # same size, and the final batch will have elements from the start of the
  # (num_epochs + 1)-th epoch.
  train_iter = itertools.islice(
      dataset.train_iterator_fn(), global_step, num_train_steps)

  eval_callbacks = []
  rng, callback_rng = jax.random.split(rng)
  callback_rngs = jax.random.split(callback_rng, len(callback_configs))
  for callback_rng, config in zip(callback_rngs, callback_configs):
    eval_callback = callbacks.get_callback(
        config['callback_name'])(model, params, batch_stats,
                                 dataset, hps, config, train_dir, callback_rng)
    eval_callbacks.append(eval_callback)


  for batch in train_iter:

    if global_step in checkpoint_steps and jax.process_index() == 0:
      checkpoint.save_unreplicated_checkpoint_background(
          checkpoint_dir,
          optimizer_state,
          params,
          batch_stats,
          metrics_state,
          global_step,
          preemption_count,
          sum_train_cost,
          max_to_keep=None)
    batch = data_utils.shard(batch)
    lr = hps.get(
        'opt_hparams.optimizers.' +
        f'{optimizer_state.inner_state.current_expert[0]}.hps.learning_rate',
        lr_fn(global_step))
    optimizer_state, params, batch_stats, cost_val, metrics_state, grad_norm = update_pmapped(
        optimizer_state,
        params,
        batch_stats,
        metrics_state,
        batch,
        global_step,
        lr,
        rng,
        local_device_indices)

    # TODO(dsuo): would be nice if this logic can live inside samuel.
    # However, need to find a way to pass back params to the trainer.
    if global_step in hps.get('opt_hparams.reinit_steps', []):
      logging.info('Syncing at step %d to expert %d',
                   global_step, optimizer_state.inner_state.current_expert[0])
      current_expert = optimizer_state.inner_state.current_expert[0]
      checkpointed_expert = current_expert
      source = jax.process_index() == current_expert

      # Unreplicate so we can broadcast a single instance of params.
      params = jax_utils.unreplicate(params)

      # multihost_utils.broadcast_one_to_all will broadcast a ShardedDeviceArray
      # but will unshard, giving an extra leading device dimension.
      params = multihost_utils.broadcast_one_to_all(params, source)

      optimizer_state = optimizer_init_fn(params)

      optimizer_state = optimizer_state._replace(
          inner_state=optimizer_state.inner_state._replace(
              current_expert=current_expert))

      # Replicate params and optimizer state so each device can use.
      params = jax_utils.replicate(params)
      optimizer_state = jax_utils.replicate(optimizer_state)

    # Calling float is needed since cost_val is a shape (1,) DeviceArray.
    sum_train_cost += float(np.mean(cost_val))
    global_step += 1
    # TODO(gdahl, gilmer): consider moving this test up.
    # NB: Since this test is after we increment global_step, having 0 in
    # eval_steps does nothing.
    if trainer_utils.should_eval(global_step, eval_frequency, eval_steps):
      # NOTE(dsuo): sync batch stats across devices for each host.
      batch_stats = _maybe_sync_batchnorm_stats(batch_stats)
      report, eval_time = eval_metrics(params, batch_stats, dataset,
                                       eval_num_batches, eval_train_num_batches,
                                       evaluate_batch_pmapped)
      # NOTE(dsuo): grab reports from all experts. For some reason, we can only
      # log metrics from one host.
      if hps.optimizer == 'samuel':
        report = gather_multihost_report(
            report, optimizer_state.inner_state.current_expert[0])
        report = add_expert_probs(report,
                                  optimizer_state.inner_state.expert_weights[0])
        report.update(
            current_expert=optimizer_state.inner_state.current_expert[0],
            checkpointed_expert=checkpointed_expert)
      mean_train_cost = sum_train_cost / max(1, global_step - prev_eval_step)
      report.update(learning_rate=float(lr),
                    global_step=global_step,
                    epoch=global_step * hps.batch_size // hps.train_size,
                    steps_per_sec=get_step_frequency(global_step),
                    eval_time=eval_time,
                    grad_norm=np.mean(grad_norm),
                    preemption_count=preemption_count,
                    train_cost=mean_train_cost)

      for eval_callback in eval_callbacks:
        callback_metrics = eval_callback.run_eval(params, batch_stats,
                                                  global_step)
        if set(callback_metrics.keys()).intersection(set(report.keys())):
          raise ValueError('There was a collision between the callback metrics'
                           'and the standard eval metrics keys')
        report.update(callback_metrics)
      yield report
      if jax.process_index() == 0:
        trainer_utils.log_epoch_report(report, metrics_logger)
        trainer_utils.maybe_log_training_metrics(metrics_state,
                                                 metrics_summary_fn,
                                                 metrics_logger)
        checkpoint.save_unreplicated_checkpoint_background(
            train_dir,
            optimizer_state,
            params,
            batch_stats,
            metrics_state,
            global_step,
            preemption_count,
            sum_train_cost)
      sum_train_cost = 0.0
      prev_eval_step = global_step

      early_stopping_condition = trainer_utils.check_for_early_stopping(
          early_stopping_target_name,
          early_stopping_target_value,
          early_stopping_mode,
          report)
      if early_stopping_condition:
        comparison_string = '>=' if early_stopping_mode == 'above' else '<='
        logging.info(
            'Early stopping because metric %s=%f, reached the target value '
            'of %s %f.',
            early_stopping_target_name,
            report[early_stopping_target_name],
            comparison_string,
            early_stopping_target_value)
        return

  # Always log and checkpoint on host 0 at the end of training.
  # If we moved where in the loop body evals happen then we would not need this
  # test.
  if prev_eval_step != num_train_steps:
    batch_stats = _maybe_sync_batchnorm_stats(batch_stats)
    report, eval_time = eval_metrics(params,
                                     batch_stats,
                                     dataset,
                                     eval_num_batches,
                                     eval_train_num_batches,
                                     evaluate_batch_pmapped)
    if hps.optimizer == 'samuel':
      report = gather_multihost_report(
          report, optimizer_state.inner_state.current_expert[0])
      report = add_expert_probs(report,
                                optimizer_state.inner_state.expert_weights[0])
      report.update(
          current_expert=optimizer_state.inner_state.current_expert[0],
          checkpointed_expert=checkpointed_expert)
    lr = lr_fn(global_step)
    # Correct the average for the final partial epoch.
    mean_train_cost = sum_train_cost / max(1, global_step - prev_eval_step)
    report.update(learning_rate=float(lr),
                  global_step=global_step,
                  epoch=global_step * hps.batch_size // hps.train_size,
                  steps_per_sec=get_step_frequency(global_step),
                  eval_time=eval_time,
                  grad_norm=np.mean(grad_norm),
                  preemption_count=preemption_count,
                  train_cost=mean_train_cost)
    yield report
    if jax.process_index() == 0:
      trainer_utils.log_epoch_report(report, metrics_logger)
      trainer_utils.maybe_log_training_metrics(metrics_state,
                                               metrics_summary_fn,
                                               metrics_logger)
      checkpoint.save_unreplicated_checkpoint_background(
          train_dir,
          optimizer_state,
          params,
          batch_stats,
          metrics_state,
          global_step,
          preemption_count,
          sum_train_cost)
  # To make sure the last checkpoint was correctly saved.
  checkpoint.wait_for_checkpoint_save()