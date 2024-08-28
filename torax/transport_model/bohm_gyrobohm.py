# Copyright 2024 DeepMind Technologies Limited
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

"""The BohmGyroBohmModel class."""

from __future__ import annotations

import dataclasses
from typing import Callable

import chex
import jax
from jax import numpy as jnp
from torax import constants as constants_module
from torax import geometry
from torax import interpolated_param
from torax import jax_utils
from torax import state
from torax.config import runtime_params_slice
from torax.transport_model import runtime_params as runtime_params_lib
from torax.transport_model import transport_model


# pylint: disable=invalid-name
@chex.dataclass
class RuntimeParams(runtime_params_lib.RuntimeParams):
  """Extends the base runtime params with additional params for this model.

  See base class runtime_params.RuntimeParams docstring for more info.
  """

  # Prefactor for Bohm term for electron heat conductivity.
  chi_e_bohm_coeff: runtime_params_lib.TimeInterpolated = 8e-5
  # Prefactor for GyroBohm term for electron heat conductivity.
  chi_e_gyrobohm_coeff: runtime_params_lib.TimeInterpolated = 5e-6
  # Prefactor for Bohm term for ion heat conductivity.
  chi_i_bohm_coeff: runtime_params_lib.TimeInterpolated = 8e-5
  # Prefactor for GyroBohm term for ion heat conductivity.
  chi_i_gyrobohm_coeff: runtime_params_lib.TimeInterpolated = 5e-6
  # Constants for the electron diffusivity weighting factor.
  d_face_c1: runtime_params_lib.TimeInterpolated = 1.0
  d_face_c2: runtime_params_lib.TimeInterpolated = 0.3

  def make_provider(
      self, torax_mesh: geometry.Grid1D | None = None
  ) -> RuntimeParamsProvider:
    return RuntimeParamsProvider(**self.get_provider_kwargs(torax_mesh))


@chex.dataclass
class RuntimeParamsProvider(runtime_params_lib.RuntimeParamsProvider):
  """Provides a RuntimeParams to use during time t of the sim."""

  runtime_params_config: RuntimeParams
  chi_e_bohm_coeff: interpolated_param.InterpolatedVarSingleAxis
  chi_e_gyrobohm_coeff: interpolated_param.InterpolatedVarSingleAxis
  chi_i_bohm_coeff: interpolated_param.InterpolatedVarSingleAxis
  chi_i_gyrobohm_coeff: interpolated_param.InterpolatedVarSingleAxis
  d_face_c1: interpolated_param.InterpolatedVarSingleAxis
  d_face_c2: interpolated_param.InterpolatedVarSingleAxis

  def build_dynamic_params(self, t: chex.Numeric) -> DynamicRuntimeParams:
    return DynamicRuntimeParams(**self.get_dynamic_params_kwargs(t))


@chex.dataclass(frozen=True)
class DynamicRuntimeParams(runtime_params_lib.DynamicRuntimeParams):
  """Dynamic runtime params for the BgB transport model."""

  chi_e_bohm_coeff: jax.Array
  chi_e_gyrobohm_coeff: jax.Array
  chi_i_bohm_coeff: jax.Array
  chi_i_gyrobohm_coeff: jax.Array
  d_face_c1: jax.Array
  d_face_c2: jax.Array

  def sanity_check(self):
    runtime_params_lib.DynamicRuntimeParams.sanity_check(self)
    jax_utils.error_if_negative(self.chi_e_bohm_coeff, 'chi_e_bohm_coeff')
    jax_utils.error_if_negative(
        self.chi_e_gyrobohm_coeff, 'chi_e_gyrobohm_coeff'
    )
    jax_utils.error_if_negative(self.chi_i_bohm_coeff, 'chi_i_bohm_coeff')
    jax_utils.error_if_negative(
        self.chi_i_gyrobohm_coeff, 'chi_i_gyrobohm_coeff'
    )
    jax_utils.error_if_negative(self.d_face_c1, 'd_face_c1')
    jax_utils.error_if_negative(self.d_face_c2, 'd_face_c2')


class BohmGyroBohmModel(transport_model.TransportModel):
  """Calculates various coefficients related to particle transport according to the Bohm + gyro-Bohm Model."""

  def __init__(
      self,
  ):
    super().__init__()
    self._frozen = True

  def _call_implementation(
      self,
      dynamic_runtime_params_slice: runtime_params_slice.DynamicRuntimeParamsSlice,
      geo: geometry.Geometry,
      core_profiles: state.CoreProfiles,
  ) -> state.CoreTransport:
    r"""Calculates transport coefficients using the BohmGyroBohm model.

    We use the implementation from Tholerus et al, Section 3.3.
    https://doi.org/10.1088/1741-4326/ad6ea2

    A description is provided in physics_models.rst.
    https://torax.readthedocs.io/en/latest/physics_models.html

    Args:
      dynamic_runtime_params_slice: Input runtime parameters that can change
        without triggering a JAX recompilation.
      geo: Geometry of the torus.
      core_profiles: Core plasma profiles.

    Returns:
      coeffs: The transport coefficients
    """
    # Many variables throughout this function are capitalized based on physics
    # notational conventions rather than on Google Python style
    # pylint: disable=invalid-name
    assert isinstance(
        dynamic_runtime_params_slice.transport, DynamicRuntimeParams
    )

    true_ne_face = (
        core_profiles.ne.face_value()
        * dynamic_runtime_params_slice.numerics.nref
    )
    true_ne_grad_face = (
        core_profiles.ne.face_grad()
        * dynamic_runtime_params_slice.numerics.nref
    )

    # Bohm term of heat transport
    chi_e_B = (
        geo.rmid_face
        * core_profiles.q_face**2
        / (constants_module.CONSTANTS.qe * geo.B0 * true_ne_face)
        * (
            jnp.abs(true_ne_grad_face) * core_profiles.temp_el.face_value()
            + jnp.abs(core_profiles.temp_el.face_grad()) * true_ne_face
        )
        * constants_module.CONSTANTS.keV2J
        / geo.rho_b
    )

    # Gyrobohm term of heat transport
    chi_e_gB = (
        jnp.sqrt(dynamic_runtime_params_slice.plasma_composition.Ai / 2)
        * jnp.sqrt(core_profiles.temp_el.face_value() * 1e3)
        / geo.B0**2
        * jnp.abs(core_profiles.temp_el.face_grad() * 1e3)
        / geo.rho_b
    )

    chi_i_B = 2 * chi_e_B
    chi_i_gB = 0.5 * chi_e_gB

    # Total heat transport
    chi_i = (
        dynamic_runtime_params_slice.transport.chi_i_bohm_coeff * chi_i_B
        + dynamic_runtime_params_slice.transport.chi_i_gyrobohm_coeff * chi_i_gB
    )
    chi_e = (
        dynamic_runtime_params_slice.transport.chi_e_bohm_coeff * chi_e_B
        + dynamic_runtime_params_slice.transport.chi_e_gyrobohm_coeff * chi_e_gB
    )

    # Electron diffusivity
    weighting = (
        dynamic_runtime_params_slice.transport.d_face_c1
        + (
            dynamic_runtime_params_slice.transport.d_face_c2
            - dynamic_runtime_params_slice.transport.d_face_c1
        )
        * geo.rho_face_norm
    )
    d_face_el = weighting * chi_e * chi_i / (chi_e + chi_i)

    # Pinch velocity
    v_face_el = (
        0.5 * d_face_el * geo.area_face**2 / (geo.volume_face * geo.vpr_face)
    )

    return state.CoreTransport(
        chi_face_ion=chi_i,
        chi_face_el=chi_e,
        d_face_el=d_face_el,
        v_face_el=v_face_el,
    )

  def __hash__(self):
    # All BohmGyroBohmModels are equivalent and can hash the same
    return hash('BohmGyroBohmModel')

  def __eq__(self, other):
    return isinstance(other, BohmGyroBohmModel)


def _default_bgb_builder() -> BohmGyroBohmModel:
  return BohmGyroBohmModel()


@dataclasses.dataclass(kw_only=True)
class BohmGyroBohmModelBuilder(transport_model.TransportModelBuilder):
  """Builds a class BohmGyroBohmModel."""

  runtime_params: RuntimeParams = dataclasses.field(
      default_factory=RuntimeParams
  )

  builder: Callable[
      [],
      BohmGyroBohmModel,
  ] = _default_bgb_builder

  def __call__(
      self,
  ) -> BohmGyroBohmModel:
    return self.builder()