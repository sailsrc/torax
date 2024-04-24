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

"""Tests time dependent boundary conditions and sources.

Ip from parameters. implicit + pereverzev-corrigan, Ti+Te+Psi, Pei standard
dens, pedestal, chi from QLKNN. Includes time dependent Ip, Ptot, and
pedestal, mocking up current-overshoot and an LH transition
"""

from torax import config as config_lib
from torax import geometry
from torax import sim as sim_lib
from torax.sources import source_config
from torax.stepper import linear_theta_method
from torax.transport_model import qlknn_wrapper


def get_config() -> config_lib.Config:
  return config_lib.Config(
      profile_conditions=config_lib.ProfileConditions(
          Ti_bound_left=10,
          Te_bound_left=10,
          Ip={0: 5, 4: 15, 6: 12, 8: 12},
          Tiped={0: 2, 4: 2, 6: 5, 8: 4},
          Teped={0: 2, 4: 2, 6: 5, 8: 4},
      ),
      numerics=config_lib.Numerics(
          current_eq=True,
          resistivity_mult=50,  # to shorten current diffusion time for the test
          bootstrap_mult=0,  # remove bootstrap current
          dtmult=150,
          maxdt=0.5,
          t_final=10,
          enable_prescribed_profile_evolution=True,
      ),
      w=0.18202270915319393,
      S_pellet_tot=0,
      S_puff_tot=0,
      S_nbi_tot=0,
      Ptot={0: 20e6, 9: 20e6, 10: 120e6, 15: 120e6},
      solver=config_lib.SolverConfig(
          predictor_corrector=False,
          use_pereverzev=True,
      ),
      sources=dict(
          fusion_heat_source=source_config.SourceConfig(
              source_type=source_config.SourceType.ZERO,
          ),
          ohmic_heat_source=source_config.SourceConfig(
              source_type=source_config.SourceType.ZERO,
          ),
      ),
  )


def get_geometry(config: config_lib.Config) -> geometry.Geometry:
  return geometry.build_chease_geometry(
      config,
      geometry_file="ITER_hybrid_citrin_equil_cheasedata.mat2cols",
      Ip_from_parameters=True,
  )


def get_transport_model() -> qlknn_wrapper.QLKNNTransportModel:
  return qlknn_wrapper.QLKNNTransportModel(
      runtime_params=qlknn_wrapper.RuntimeParams(
          apply_inner_patch=True,
          chii_inner=2.0,
          chie_inner=2.0,
          rho_inner=0.3,
      ),
  )


def get_sim() -> sim_lib.Sim:
  # This approach is currently lightweight because so many objects require
  # config for construction, but over time we expect to transition to most
  # config taking place via constructor args in this function.
  config = get_config()
  geo = get_geometry(config)
  return sim_lib.build_sim_from_config(
      config=config,
      geo=geo,
      stepper_builder=linear_theta_method.LinearThetaMethod,
      transport_model=get_transport_model(),
  )