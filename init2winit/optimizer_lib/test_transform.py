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

"""Tests for transforms."""

from typing import NamedTuple

from absl.testing import absltest
import chex
from init2winit.optimizer_lib.optmaximus import kitchen_sink
import jax
import jax.numpy as jnp
import optax


def _optimizer_loop(optimizer, iterations=5):
  """Helper function for running optimizer loops."""
  params = {'w': jnp.ones((2,))}
  opt_state = optimizer.init(params)
  results = []
  for _ in range(iterations):
    compute_loss = lambda params, x, y: optax.l2_loss(params['w'].dot(x), y)
    grads = jax.grad(compute_loss)(params, jnp.array([5.0, 6.0]), 4.0)
    updates, opt_state = optimizer.update(grads, opt_state)
    params = optax.apply_updates(params, updates)
    results.append(params)
  return results


class NesterovTest(chex.TestCase):
  """Test correctness of nesterov momentum."""

  def test_correctness(self):
    """Testing correctness via an independent flax.optim run."""

    target_solution = [
        {
            'w': jnp.array([0.40500003, 0.286])
        },
        {
            'w': jnp.array([0.255515, 0.106618])
        },
        {
            'w': jnp.array([0.31884143, 0.18260972])
        },
        {
            'w': jnp.array([0.40163627, 0.28196353])
        },
        {
            'w': jnp.array([0.43924114, 0.32708937])
        },
    ]
    optimizer = kitchen_sink(['nesterov'], [{'decay': 0.7}], learning_rate=0.01)
    results = _optimizer_loop(optimizer)
    for target, result in zip(target_solution, results):
      chex.assert_trees_all_close(target, result)


class PolyakHBTest(chex.TestCase):
  """Test correctness of polyak_hb momentum."""

  def test_correctness(self):
    """Testing correctness via an independent flax.optim run."""

    target_solution = [
        {
            'w': jnp.array([0.65, 0.58000004])
        },
        {
            'w': jnp.array([0.26849997, 0.12220004])
        },
        {
            'w': jnp.array([0.09766498, -0.08280197])
        },
        {
            'w': jnp.array([0.17850482, 0.01420582])
        },
        {
            'w': jnp.array([0.38620475, 0.2634457])
        },
    ]
    optimizer = kitchen_sink(['polyak_hb'], [{
        'decay': 0.7
    }],
                             learning_rate=0.01)
    results = _optimizer_loop(optimizer)
    for target, result in zip(target_solution, results):
      chex.assert_trees_all_close(target, result)


class PolyakEMATest(chex.TestCase):
  """Test correctness of polyak_ema momentum."""

  def test_correctness(self):
    """Testing correctness via independent implementation."""

    def ema(decay):

      def init_fn(params):
        del params
        return {'w': jnp.zeros((2,))}

      def update_fn(updates, state, params=None):
        del params
        state['w'] = ((1 - decay) * updates['w'] + decay * state['w'])
        return state, state

      return optax.GradientTransformation(init_fn, update_fn)

    decay = 0.7
    learning_rate = 0.01
    true_ema = optax.chain(ema(decay), optax.scale(-1. * learning_rate))
    ks_ema = kitchen_sink(['polyak_ema'], [{
        'decay': decay
    }],
                          learning_rate=learning_rate)
    targets = _optimizer_loop(true_ema)
    results = _optimizer_loop(ks_ema)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)


class PreconditionByAdamTest(chex.TestCase):
  """Test correctness of precondition_by_adam."""

  def test_debias_false(self):
    rms_prop = kitchen_sink(['scale_by_rms'])
    precondition_by_adam = kitchen_sink(['precondition_by_adam'], [{
        'eps': 0,
        'eps_root': 1e-8,
        'b2': 0.9,
        'debias': False
    }])
    targets = _optimizer_loop(rms_prop)
    results = _optimizer_loop(precondition_by_adam)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)

  def test_debias_true(self):
    adam = kitchen_sink(['scale_by_adam'], [{'b1': 0.0}])
    precondition_by_adam = kitchen_sink(['precondition_by_adam'])
    targets = _optimizer_loop(adam)
    results = _optimizer_loop(precondition_by_adam)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)


class TwistedAdamTest(chex.TestCase):
  """Test correctness of twisted_adam."""

  def test_correctness(self):
    """Testing correctness via independent implementation."""

    rms_decay = 0.9
    rms_eps = 1e-8
    rms_scale = 0.

    bias_decay = 0.1

    polyak_decay = 0.1

    class State(NamedTuple):
      nu: optax.Updates
      trace: optax.Params
      count: chex.Array

    def twisted_adam():

      def init_fn(params):
        return State(
            nu=jax.tree_map(lambda n: jnp.full_like(n, rms_scale), params),
            trace=jax.tree_map(jnp.zeros_like, params),
            count=jnp.zeros([], jnp.int32))

      def update_fn(updates, state, params=None):
        del params
        count = state.count + jnp.array(1, jnp.int32)
        nu = {
            'w': (1 - rms_decay) * (updates['w']**2) + rms_decay * state.nu['w']
        }
        updates = {'w': updates['w'] * jax.lax.rsqrt(nu['w'] + rms_eps)}

        updates = {'w': updates['w'] / (1 - bias_decay**count)}

        trace = {'w': updates['w'] + polyak_decay * state.trace['w']}
        updates = {'w': trace['w']}

        updates = {'w': updates['w'] / (1 - bias_decay**count)}

        return updates, State(nu=nu, count=count, trace=trace)

      return optax.GradientTransformation(init_fn, update_fn)

    true_twisted_adam = twisted_adam()
    ks_twisted_adam = kitchen_sink(
        ['scale_by_rms', 'bias_correction', 'polyak_hb', 'bias_correction'], [
            {
                'decay': rms_decay,
                'eps': rms_eps,
                'initial_scale': rms_scale,
            },
            {
                'decay': bias_decay
            },
            {
                'decay': polyak_decay
            },
            {
                'decay': bias_decay
            },
        ])

    targets = _optimizer_loop(true_twisted_adam)
    results = _optimizer_loop(ks_twisted_adam)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)


class AMSGradTest(chex.TestCase):
  """Test correctness of scale_by_amsgrad."""

  def test_correctness(self):
    """Testing correctness via optax.adam."""

    def amsgrad():
      adam = optax.scale_by_adam()

      def init_fn(params):
        return adam.init(params)

      def update_fn(updates, state, params=None):
        prev_nu = state.nu
        _, state = adam.update(updates, state, params)
        curr_nu = state.nu
        nu_hat = jax.tree_multimap(jnp.maximum, curr_nu, prev_nu)
        updates = jax.tree_multimap(lambda m, v: m / (jnp.sqrt(v + 0.0) + 1e-8),
                                    state.mu, nu_hat)

        return updates, optax.ScaleByAdamState(
            count=state.count, mu=state.mu, nu=nu_hat)

      return optax.GradientTransformation(init_fn, update_fn)

    true_amsgrad = amsgrad()
    ks_amsgrad = kitchen_sink(['scale_by_amsgrad'])

    targets = _optimizer_loop(true_amsgrad)
    results = _optimizer_loop(ks_amsgrad)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)


class EquivalenceTest(chex.TestCase):
  """Test equivalence of kitchen_sink and optax adagrad."""

  def test_adagrad(self):
    true_adagrad = optax.adagrad(0.7, initial_accumulator_value=0.3)
    ks_adagrad = kitchen_sink(['scale_by_rss', 'polyak_ema'], [{
        'initial_accumulator_value': 0.3
    }, {
        'decay': 0.0
    }],
                              learning_rate=0.7)

    targets = _optimizer_loop(true_adagrad)
    results = _optimizer_loop(ks_adagrad)

    for target, result in zip(targets, results):
      chex.assert_trees_all_close(target, result)


if __name__ == '__main__':
  absltest.main()