# coding=utf-8
# Copyright 2023 The init2winit Authors.
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

"""Trainers for init2winit."""

from init2winit.trainer_lib import data_selection_trainer
from init2winit.trainer_lib import distillation_trainer
from init2winit.trainer_lib import quantization_trainer
from init2winit.trainer_lib import trainer


_ALL_TRAINERS = {
    'standard': trainer.Trainer,
    'quantization': quantization_trainer.Trainer,
    'distillation': distillation_trainer.Trainer,
    'data_selection': data_selection_trainer.DataSelectionTrainer,
}


def get_trainer_cls(trainer_name):
  """Maps trainer name to a Trainer class."""
  try:
    return _ALL_TRAINERS[trainer_name]
  except KeyError:
    raise ValueError('Unrecognized trainer: {}'.format(trainer_name)) from None

