# coding=utf-8
# Copyright 2021 The init2winit Authors.
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

"""Defines the SparseInit initializer.

This initializer limits the number of non-zero incoming connection weights.
For more information, see Section 5 of (Martens, 2010), which can be found at
https://www.cs.toronto.edu/~jmartens/docs/Deep_HessianFree.pdf.
"""

from jax.ops import index
from jax.ops import index_update
from ml_collections.config_dict import config_dict
import numpy as np

DEFAULT_HPARAMS = config_dict.ConfigDict(dict(non_zero_connection_weights=15,))


def sparse_init(loss_fn,
                model,
                hps,
                input_shape,
                output_shape,
                rng_key,
                metrics_logger=None,
                log_every=10):
  """Implements SparseInit initializer.

  Args:
    loss_fn: Loss function.
    model: Flax Model class.
    hps: HParam object. Required hparams are meta_learning_rate,
      meta_batch_size, meta_steps, and epsilon.
    input_shape: Must agree with batch[0].shape[1:].
    output_shape: Must agree with batch[1].shape[1:].
    rng_key: jax.PRNGKey, used to seed all randomness.
    metrics_logger: Instance of utils.MetricsLogger
    log_every: Print meta loss every k steps.

  Returns:
    A Flax model with sparse initialization.
  """

  del loss_fn, input_shape, output_shape, rng_key, metrics_logger, log_every

  activation_functions = hps.activation_function
  num_hidden_layers = len(hps.hid_sizes)
  if isinstance(hps.activation_function, str):
    activation_functions = [hps.activation_function] * num_hidden_layers
  for i, key in enumerate(model.params):
    num_units, num_weights = model.params[key]['kernel'].shape
    mask = np.zeros((num_units, num_weights), dtype=bool)
    for k in range(num_units):
      if num_weights >= hps.non_zero_connection_weights:
        sample = np.random.choice(
            num_weights, hps.non_zero_connection_weights, replace=False)
      else:
        sample = np.random.choice(num_weights, hps.non_zero_connection_weights)
      mask[k, sample] = True
    model.params[key]['kernel'] = index_update(model.params[key]['kernel'],
                                               index[~mask], 0.0)
    if i < num_hidden_layers and activation_functions[i] == 'tanh':
      model.params[key]['bias'] = index_update(model.params[key]['bias'],
                                               index[:], 0.5)
    else:
      model.params[key]['bias'] = index_update(model.params[key]['bias'],
                                               index[:], 0.0)
  return model