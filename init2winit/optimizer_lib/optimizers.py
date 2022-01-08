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

"""Getter function for selecting optimizers."""

from init2winit.optimizer_lib.hessian_free import hessian_free
import numpy as np
import optax


def sgd(learning_rate, weight_decay, momentum=None, nesterov=False):
  r"""A customizable gradient descent optimizer.

  NOTE: We apply weight decay **before** computing the momentum update.
  This is equivalent to applying WD after for heavy-ball momentum,
  but slightly different when using Nesterov accelleration. This is the same as
  how the Flax optimizers handle weight decay
  https://flax.readthedocs.io/en/latest/_modules/flax/optim/momentum.html.

  Args:
    learning_rate: The learning rate. Expected as the positive learning rate,
      for example `\alpha` in `w -= \alpha * u` (as opposed to `\alpha`).
    weight_decay: The weight decay hyperparameter.
    momentum: The momentum hyperparameter.
    nesterov: Whether or not to use Nesterov momentum.

  Returns:
    An optax gradient transformation that applies weight decay and then one of a
    {SGD, Momentum, Nesterov} update.
  """
  return optax.chain(
      optax.add_decayed_weights(weight_decay),
      optax.sgd(
          learning_rate=learning_rate,
          momentum=momentum,
          nesterov=nesterov)
  )


def get_optimizer(hps, model=None):
  """Constructs the optax optimizer from the given HParams.

  We use optax.inject_hyperparams to wrap the optimizer transformations that
  accept learning rates. This allows us to "inject" the learning rate at each
  step in a training loop by manually setting it in the optimizer_state,
  calculating it using whatever (Python or Jax) logic we want. This is why we
  set learning_rate=0.0 for all optimizers below. Note that all optax
  transformations returned from this function need to have
  `optax.inject_hyperparams` as the top level transformation.

  Args:
    hps: the experiment hyperparameters, as a ConfigDict.
    model: the model to be trained.
  Returns:
    A tuple of the initialization and update functions returned by optax.
  """
  # We handle hps.l2_decay_factor in the training cost function base_model.py
  # and hps.weight_decay in the optimizer. It is almost certainly an error if
  # both are set.
  weight_decay = hps.opt_hparams.get('weight_decay', 0)
  assert hps.l2_decay_factor is None or weight_decay == 0.0

  opt_init = None
  opt_update = None

  if hps.optimizer == 'sgd':
    opt_init, opt_update = optax.inject_hyperparams(sgd)(
        learning_rate=0.0,  # Manually injected on each train step.
        weight_decay=weight_decay)
  elif hps.optimizer == 'momentum' or hps.optimizer == 'nesterov':
    opt_init, opt_update = optax.inject_hyperparams(sgd)(
        learning_rate=0.0,  # Manually injected on each train step.
        weight_decay=weight_decay,
        momentum=hps.opt_hparams['momentum'],
        nesterov=(hps.optimizer == 'nesterov'))
  elif hps.optimizer == 'adam':
    opt_init, opt_update = optax.inject_hyperparams(optax.adamw)(
        learning_rate=0.0,  # Manually injected on each train step.
        b1=hps.opt_hparams['beta1'],
        b2=hps.opt_hparams['beta2'],
        eps=hps.opt_hparams['epsilon'],
        weight_decay=weight_decay)
  elif hps.optimizer == 'hessian_free':
    if model is None:
      raise ValueError(
          'Model info should be provided for hessian free optimizer.')
    opt_init, opt_update = optax.inject_hyperparams(
        hessian_free,
        ['flax_module_def', 'loss_fn', 'max_iter'])(
            flax_module_def=model.flax_module_def,
            loss_fn=model.loss_fn,
            learning_rate=0.0,  # Manually injected on each train step.
            max_iter=np.prod(hps.output_shape))

  if opt_init is None or opt_update is None:
    raise NotImplementedError('Optimizer {} not implemented'.format(
        hps.optimizer))
  return opt_init, _wrap_update_fn(hps.optimizer, opt_update)


def _wrap_update_fn(opt_name, opt_update):
  """Wraps the optimizer update function to have the same function signiture.

  Args:
    opt_name: the optimizer name.
    opt_update: the optimizer update function.
  Returns:
    A wrapped optimizer update function.
  """
  def update_fn(grads, optimizer_state, params, batch=None, batch_stats=None):
    if opt_name == 'hessian_free':
      variables = {'params': params}
      if batch_stats is not None:
        variables['batch_stats'] = batch_stats
      return opt_update(grads, optimizer_state, params=(variables, batch))
    return opt_update(grads, optimizer_state, params=params)
  return update_fn
