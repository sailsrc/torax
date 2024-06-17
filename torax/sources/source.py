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

"""Module for a single source/sink term.

This module contains all the base classes for defining source terms. Other files
in this folder use these classes to define specific types of sources/sinks.

See Source class docstring for more details on what a TORAX source is and how to
use it.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Callable, Protocol

import chex
from jax import numpy as jnp
from torax import geometry
from torax import jax_utils
from torax import state
from torax.config import runtime_params_slice
from torax.sources import runtime_params as runtime_params_lib


# Sources implement these functions to be able to provide source profiles.
SourceProfileFunction = Callable[
    [  # Arguments
        runtime_params_slice.DynamicRuntimeParamsSlice,  # General config params
        runtime_params_lib.DynamicRuntimeParams,  # Source-specific params.
        geometry.Geometry,
        state.CoreProfiles | None,
    ],
    # Returns a JAX array, tuple of arrays, or mapping of arrays.
    chex.ArrayTree,
]


# Any callable which takes the dynamic runtime_params, geometry, and optional
# core profiles, and outputs a shape corresponding to the expected output of a
# source. See how these types of functions are used in the Source class below.
SourceOutputShapeFunction = Callable[
    [  # Arguments
        geometry.Geometry,
    ],
    # Returns shape of the source's output.
    tuple[int, ...],
]


def get_cell_profile_shape(
    geo: geometry.Geometry,
):
  """Returns the shape of a source profile on the cell grid."""
  return ProfileType.CELL.get_profile_shape(geo)


@enum.unique
class AffectedCoreProfile(enum.IntEnum):
  """Defines which part of the core profiles the source helps evolve.

  The profiles of each source/sink are terms included in equations evolving
  different core profiles. This enum maps a source to those equations.
  """

  # Source profile is not used for any core profile equation
  NONE = 0
  # Current density equation.
  PSI = 1
  # Electron density equation.
  NE = 2
  # Ion temperature equation.
  TEMP_ION = 3
  # Electron temperature equation.
  TEMP_EL = 4


@dataclasses.dataclass(kw_only=True)
class Source:
  """Base class for a single source/sink term.

  Sources are used to compute source profiles (see source_profiles.py), which
  are in turn used to compute coeffs in sim.py.

  NOTE: For most use cases, you should extend or use SingleProfileSource.

  Attributes:
    runtime_params: Input dataclass containing all the source-specific runtime
      parameters. At runtime, the parameters here are interpolated to a specific
      time t and then passed to the model_func or formula, depending on the mode
      this source is running in.
    affected_core_profiles: Core profiles affected by this source's profile(s).
      This attribute defines which equations the source profiles are terms for.
      By default, the number of affected core profiles should equal the rank of
      the output shape returned by output_shape_getter. Subclasses may override
      this requirement.
    supported_modes: Defines how the source computes its profile. Can be set to
      zero, model-based, etc. At runtime, the input runtime config (the Config
      or the DynamicConfigSlice) will specify which supported type the Source is
      running with. If the runtime config specifies an unsupported type, an
      error will raise.
    output_shape_getter: Callable which returns the shape of the profiles given
      by this source.
    model_func: The function used when the the runtime type is set to
      "MODEL_BASED". If not provided, then it defaults to returning zeros.
    formula: The prescribed formula used when the runtime type is set to
      "FORMULA_BASED". If not provided, then it defaults to returning zeros.
    affected_core_profiles_ints: Derived property from the
      affected_core_profiles. Integer values of those enums.
  """

  affected_core_profiles: tuple[AffectedCoreProfile, ...]

  supported_modes: tuple[runtime_params_lib.Mode, ...] = (
      runtime_params_lib.Mode.ZERO,
      runtime_params_lib.Mode.FORMULA_BASED,
  )

  output_shape_getter: SourceOutputShapeFunction = get_cell_profile_shape

  model_func: SourceProfileFunction | None = None

  formula: SourceProfileFunction | None = None

  @property
  def affected_core_profiles_ints(self) -> tuple[int, ...]:
    return tuple([int(cp) for cp in self.affected_core_profiles])

  def check_mode(
      self,
      mode: int | jnp.ndarray,
  ) -> jnp.ndarray:
    """Raises an error if the source type is not supported."""
    # This function is really just a wrapper around jax_utils.error_if with the
    # custom error message coming from this class.
    mode = jnp.array(mode)
    mode = jax_utils.error_if(
        mode,
        jnp.logical_not(self._is_type_supported(mode)),
        self._unsupported_mode_error_msg(mode),
    )
    return mode  # pytype: disable=bad-return-type

  def _is_type_supported(
      self,
      mode: int | jnp.ndarray,
  ) -> jnp.ndarray:
    """Returns whether the source type is supported."""
    mode = jnp.array(mode)
    return jnp.any(
        jnp.bool_([
            supported_mode.value == mode
            for supported_mode in self.supported_modes
        ])
    )

  def _unsupported_mode_error_msg(
      self,
      mode: runtime_params_lib.Mode | int | jnp.ndarray,
  ) -> str:
    return (
        f'This source supports the following modes: {self.supported_modes}.'
        f' Unsupported mode provided: {mode}.'
    )

  def get_value(
      self,
      dynamic_runtime_params_slice: runtime_params_slice.DynamicRuntimeParamsSlice,
      dynamic_source_runtime_params: runtime_params_lib.DynamicRuntimeParams,
      geo: geometry.Geometry,
      core_profiles: state.CoreProfiles | None = None,
  ) -> chex.ArrayTree:
    """Returns the profile for this source during one time step.

    Args:
      dynamic_runtime_params_slice: Slice of the general TORAX config that can
        be used as input for this time step.
      dynamic_source_runtime_params: Slice of this source's runtime parameters
        at a specific time t.
      geo: Geometry of the torus.
      core_profiles: Core plasma profiles. May be the profiles at the start of
        the time step or a "live" set of core profiles being actively updated
        depending on whether this source is explicit or implicit. Explicit
        sources get the core profiles at the start of the time step, implicit
        sources get the "live" profiles that is updated through the course of
        the time step as the solver converges.

    Returns:
      Array, arrays, or nested dataclass/dict of arrays for the source profile.
    """
    self.check_mode(dynamic_source_runtime_params.mode)
    output_shape = self.output_shape_getter(geo)
    model_func = (
        (lambda _0, _1, _2, _3: jnp.zeros(output_shape))
        if self.model_func is None
        else self.model_func
    )
    formula = (
        (lambda _0, _1, _2, _3: jnp.zeros(output_shape))
        if self.formula is None
        else self.formula
    )
    return get_source_profiles(
        dynamic_runtime_params_slice=dynamic_runtime_params_slice,
        dynamic_source_runtime_params=dynamic_source_runtime_params,
        geo=geo,
        core_profiles=core_profiles,
        model_func=model_func,
        formula=formula,
        output_shape=output_shape,
    )

  def get_source_profile_for_affected_core_profile(
      self,
      profile: chex.ArrayTree,
      affected_core_profile: int,
      geo: geometry.Geometry,
  ) -> jnp.ndarray:
    """Returns the part of the profile to use for the given core profile.

    A single source can output profiles used as terms in more than one equation
    while evolving the core profiles (for instance, it can output profiles for
    both the ion temperature and electron temperature equations).

    Users of this source, though, may need to grab the specific parts of the
    output (from get_value()) that relate to a specific core profile.

    This function helps do that. By default, it returns the input profile as is
    if the requested core profile is valid, otherwise returns zeros.

    NOTE: This function assumes the ArrayTree returned by get_value() is a JAX
    array with shape (num affected core profiles, cell grid length) and that the
    order of the arrays in the output match the order of the
    affected_core_profile attribute.

    Subclasses can override this behavior to fit the type of ArrayTree they
    output.

    Args:
      profile: The profile output from get_value().
      affected_core_profile: The specific core profile we want to pull the
        profile for. This is the integer value of the enum AffectedCoreProfile
        because enums are not JAX-friendly as function arguments. If it is not
        one of the core profiles this source actually affects, this will return
        zeros.
      geo: Geometry of the torus.

    Returns: The source profile on the cell grid for the requested core profile.
    """
    # Get a valid index that defaults to 0 if not present.
    affected_core_profile_ints = self.affected_core_profiles_ints
    idx = jnp.argmax(
        jnp.asarray(affected_core_profile_ints) == affected_core_profile
    )
    return jnp.where(
        affected_core_profile in affected_core_profile_ints,
        profile[idx, ...],
        jnp.zeros_like(geo.r),
    )


@dataclasses.dataclass(kw_only=True)
class SingleProfileSource(Source):
  """Source providing a single output profile on the cell grid.

  Most sources in TORAX are instances (or subclasses) of this class.

  You can define custom sources inline when constructing the full list of
  sources to use in TORAX.

  .. code-block:: python

    # Define an electron-density source with a Gaussian profile.
    my_custom_source = source.SingleProfileSource(
        supported_modes=(
            runtime_params_lib.Mode.ZERO,
            runtime_params_lib.Mode.FORMULA_BASED,
        ),
        affected_core_profiles=[source.AffectedCoreProfile.NE],
        formula=formulas.Gaussian(my_custom_source_name),
    )
    # Define its runtime parameters (this could be done in the constructor as
    # well).
    my_custom_source.runtime_params = runtime_params_lib.RuntimeParams(
        mode=runtime_params_lib.Mode.FORMULA_BASED,
        formula=formula_config.Gaussian(
            total=1.0,
            c1=2.0,
            c2=3.0,
        ),
    )
    all_torax_sources = source_models_lib.SourceModels(
        sources={
            'my_custom_source': my_custom_source,
        }
    )

  If you want to create a subclass of SingleProfileSource with frozen
  parameters, you can provide default implementations/attributes. This is an
  example of a model-based source with a frozen custom model that cannot be
  changed by a runtime_params, along with custom runtime parameters specific to
  this
  source:

  .. code-block:: python

    @dataclasses.dataclass(kw_only=True)
    class FooRuntimeParams(runtime_params_lib.RuntimeParams):
      foo_param: runtime_params_lib.TimeDependentField
      bar_param: float

      def (build_dynamic_params(self, t: chex.Numeric)
      -> DynamicFooRuntimeParams):
      return DynamicFooRuntimeParams(
          **config_args.get_init_kwargs(
              input_config=self,
              output_type=DynamicFooRuntimeParams,
              t=t,
          )
      )

    @chex.dataclass(frozen=True)
    class DynamicFooRuntimeParams(runtime_params_lib.DynamicRuntimeParams):
      foo_param: float
      bar_param: float

    def _my_foo_model(
        dynamic_runtime_params_slice,
        dynamic_source_runtime_params,
        geo,
        core_profiles,
    ) -> jnp.ndarray:
      assert isinstance(dynamic_source_runtime_params, DynamicFooRuntimeParams)
      # implement your foo model.

    @dataclasses.dataclass(kw_only=True)
    class FooSource(SingleProfileSource):

      # Provide a default set of params.
      runtime_params: FooRuntimeParams = dataclasses.field(
          default_factory=lambda: FooRuntimeParams(
              foo_param={0.0: 10.0, 1.0: 20.0, 2.0: 35.0},
              bar_param: 1.234,
          )
      )

      # By default, FooSource's can be model-based or set to 0.
      supported_modes: tuple[runtime_params_lib.Mode, ...] = (
          runtime_params_lib.Mode.ZERO,
          runtime_params_lib.Mode.MODEL_BASED,
      )

      # Don't include model_func in the __init__ arguments and freeze it.
      model_func: SourceProfileFunction = dataclasses.field(
          init=False,
          default_factory=lambda: _my_foo_model,
      )
  """

  # Don't include output_shape_getter in the __init__ arguments.
  # Freeze this parameter so that it always outputs a single cell profile.
  output_shape_getter: SourceOutputShapeFunction = dataclasses.field(
      init=False,
      default_factory=lambda: get_cell_profile_shape,
  )

  def get_value(
      self,
      dynamic_runtime_params_slice: runtime_params_slice.DynamicRuntimeParamsSlice,
      dynamic_source_runtime_params: runtime_params_lib.DynamicRuntimeParams,
      geo: geometry.Geometry,
      core_profiles: state.CoreProfiles | None = None,
  ) -> jnp.ndarray:
    """Returns the profile for this source during one time step."""
    output_shape = self.output_shape_getter(geo)
    profile = super().get_value(
        dynamic_runtime_params_slice=dynamic_runtime_params_slice,
        dynamic_source_runtime_params=dynamic_source_runtime_params,
        geo=geo,
        core_profiles=core_profiles,
    )
    assert isinstance(profile, jnp.ndarray)
    chex.assert_rank(profile, 1)
    chex.assert_shape(profile, output_shape)
    return profile

  def get_source_profile_for_affected_core_profile(
      self,
      profile: chex.ArrayTree,
      affected_core_profile: int,
      geo: geometry.Geometry,
  ) -> jnp.ndarray:
    return jnp.where(
        affected_core_profile in self.affected_core_profiles_ints,
        profile,
        jnp.zeros_like(geo.r),
    )


class ProfileType(enum.Enum):
  """Describes what kind of profile is expected from a source."""

  # Source should return a profile on the cell grid.
  CELL = enum.auto()

  # Source should return a profile on the face grid.
  FACE = enum.auto()

  def get_profile_shape(self, geo: geometry.Geometry) -> tuple[int, ...]:
    """Returns the expected length of the source profile."""
    profile_type_to_len = {
        ProfileType.CELL: geo.r.shape,
        ProfileType.FACE: geo.r_face.shape,
    }
    return profile_type_to_len[self]

  def get_zero_profile(self, geo: geometry.Geometry) -> jnp.ndarray:
    """Returns a source profile with all zeros."""
    return jnp.zeros(self.get_profile_shape(geo))


def get_source_profiles(
    dynamic_runtime_params_slice: runtime_params_slice.DynamicRuntimeParamsSlice,
    dynamic_source_runtime_params: runtime_params_lib.DynamicRuntimeParams,
    geo: geometry.Geometry,
    core_profiles: state.CoreProfiles | None,
    model_func: SourceProfileFunction,
    formula: SourceProfileFunction,
    output_shape: tuple[int, ...],
) -> jnp.ndarray:
  """Returns source profiles requested by the runtime_params_lib.

  This function handles MODEL_BASED, FORMULA_BASED, and ZERO sources. All other
  source types will be ignored.

  Args:
    dynamic_runtime_params_slice: Slice of the general TORAX config that can be
      used as input for this time step.
    dynamic_source_runtime_params: Slice of this source's runtime parameters at
      a specific time t.
    geo: Geometry information. Used as input to the source profile functions.
    core_profiles: Core plasma profiles. Used as input to the source profile
      functions.
    model_func: Model function.
    formula: Formula implementation.
    output_shape: Expected shape of the outut array.

  Returns:
    Output array of a profile or concatenated/stacked profiles.
  """
  mode = dynamic_source_runtime_params.mode
  zeros = jnp.zeros(output_shape)
  output = jnp.zeros(output_shape)
  output += jnp.where(
      mode == runtime_params_lib.Mode.MODEL_BASED.value,
      model_func(
          dynamic_runtime_params_slice,
          dynamic_source_runtime_params,
          geo,
          core_profiles,
      ),
      zeros,
  )
  output += jnp.where(
      mode == runtime_params_lib.Mode.FORMULA_BASED.value,
      formula(
          dynamic_runtime_params_slice,
          dynamic_source_runtime_params,
          geo,
          core_profiles,
      ),
      zeros,
  )
  return output


# Convenience classes to reduce a little boilerplate for some of the common
# sources defined in the other files in this folder.


@dataclasses.dataclass(kw_only=True)
class SingleProfilePsiSource(SingleProfileSource):

  affected_core_profiles: tuple[AffectedCoreProfile, ...] = (
      AffectedCoreProfile.PSI,
  )


@dataclasses.dataclass(kw_only=True)
class SingleProfileNeSource(SingleProfileSource):

  affected_core_profiles: tuple[AffectedCoreProfile, ...] = (
      AffectedCoreProfile.NE,
  )


@dataclasses.dataclass(kw_only=True)
class SingleProfileTempIonSource(SingleProfileSource):

  affected_core_profiles: tuple[AffectedCoreProfile, ...] = (
      AffectedCoreProfile.TEMP_ION,
  )


@dataclasses.dataclass(kw_only=True)
class SingleProfileTempElSource(SingleProfileSource):

  affected_core_profiles: tuple[AffectedCoreProfile, ...] = (
      AffectedCoreProfile.TEMP_EL,
  )


def _get_ion_el_output_shape(geo):
  return (2,) + ProfileType.CELL.get_profile_shape(geo)


@dataclasses.dataclass(kw_only=True)
class IonElectronSource(Source):
  """Base class for a source/sink that can be used for both ions / electrons.

  Some ion and electron heat sources share a lot of computation resulting in
  values that are often simply proportionally scaled versions of the other. To
  help with defining those sources where you'd like to (a) keep the values
  similar and (b) get some small efficiency gain by doing some computations
  once instead of twice (once for ions and again for electrons), this class
  gives a hook for doing that.

  This class is set to always return 2 source profiles on the cell grid, the
  first being ion profile and the second being the electron profile.
  """

  supported_modes: tuple[runtime_params_lib.Mode, ...] = (
      runtime_params_lib.Mode.FORMULA_BASED,
      runtime_params_lib.Mode.ZERO,
  )

  # Don't include affected_core_profiles in the __init__ arguments.
  # Freeze this param.
  affected_core_profiles: tuple[AffectedCoreProfile, ...] = dataclasses.field(
      init=False,
      default=(AffectedCoreProfile.TEMP_ION, AffectedCoreProfile.TEMP_EL,),
  )

  # Don't include output_shape_getter in the __init__ arguments.
  # Freeze this parameter so that it always outputs 2 cell profiles.
  output_shape_getter: SourceOutputShapeFunction = dataclasses.field(
      init=False,
      default_factory=lambda: _get_ion_el_output_shape,
  )

  def get_value(
      self,
      dynamic_runtime_params_slice: runtime_params_slice.DynamicRuntimeParamsSlice,
      dynamic_source_runtime_params: runtime_params_lib.DynamicRuntimeParams,
      geo: geometry.Geometry,
      core_profiles: state.CoreProfiles | None = None,
  ) -> jnp.ndarray:
    """Computes the ion and electron values of the source.

    Args:
      dynamic_runtime_params_slice: Input config which can change from time step
        to time step.
      dynamic_source_runtime_params: Slice of this source's runtime parameters
        at a specific time t.
      geo: Geometry of the torus.
      core_profiles: Core plasma profiles used to compute the source's profiles.

    Returns:
      2 stacked arrays, the first for the ion profile and the second for the
      electron profile.
    """
    output_shape = self.output_shape_getter(geo)
    profile = super().get_value(
        dynamic_runtime_params_slice=dynamic_runtime_params_slice,
        dynamic_source_runtime_params=dynamic_source_runtime_params,
        geo=geo,
        core_profiles=core_profiles,
    )
    assert isinstance(profile, jnp.ndarray)
    chex.assert_rank(profile, 2)
    chex.assert_shape(profile, output_shape)
    return profile


class SourceBuilderProtocol(Protocol):
  """Make a best effort to define what SourceBuilders are with type hints.

  Note that these can't be used with `isinstance` or any other runtime
  evaluation, just static analysis.

  Attributes:
    runtime_params: Mutable runtime params that will continue to control the
      immutable Source after the Source has been built.
    links_back: If True, the Source will have a `source_models` field linking
      back to its SourceModels.
  """

  runtime_params: Any
  links_back: bool

  def __call__(self, *args: Any, **kwargs: Any) -> Any:
    # pylint: disable = g-doc-args
    """When called, the SourceBuilder builds a Source.

    This signature is used just to make pytype recognize SourceBuilders are
    callable. Actual SourceBuilders take either no args or if `links_back`
    they take a `source_models` argument.
    """
    ...


def is_source_builder(obj, raise_if_false: bool = False) -> bool:
  """Runtime type guard function for source builders.

  Args:
    obj: The object to type check.
    raise_if_false: If true, raises a TypeError explaining why the object is not
      a Source Builder.

  Returns:
    bool: True if `obj` is a valid source builder
  """
  if not dataclasses.is_dataclass(obj):
    if raise_if_false:
      raise TypeError('Not a dataclass')
    return False
  if not hasattr(obj, 'runtime_params'):
    if raise_if_false:
      raise TypeError('Has no runtime_params')
    return False
  if not callable(obj):
    if raise_if_false:
      raise TypeError('Not callable')
    return False
  return True


def _convert_source_builder_to_init_kwargs(
    source_builder: ...,
) -> dict[str, Any]:
  """Returns a dict of init kwargs for the source builder."""
  source_init_kwargs = {}
  for field in dataclasses.fields(source_builder):
    if field.name == 'runtime_params':
      continue
    # for loop with getattr copies each field exactly as it exists.
    # dataclasses.asdict will recursivesly convert fields to dicts,
    # including turning custom dataclasses with __call__ methods into
    # plain Python dictionaries.
    source_init_kwargs[field.name] = getattr(source_builder, field.name)
  return source_init_kwargs


def make_source_builder(
    source_type: ...,
    runtime_params_type: ... = runtime_params_lib.RuntimeParams,
    links_back=False,
) -> SourceBuilderProtocol:
  """Given a Source type, returns a Builder for that type.

  Builders are factories that also hold dynamic runtime parameters.

  Args:
    source_type: The Source class to make a builder for.
    runtime_params_type: The type of `runtime_params` field which will be added
      to the builder dataclass.
    links_back: If True, the Source class has a `source_models` field linking
      back to the SourceModels object. This must be passed to the builder's
      __call__ method.

  Returns:
    builder: a Builder dataclass for the given Source dataclass.
  """

  source_fields = dataclasses.fields(source_type)

  # Runtime params are mutable and must be in the builder only.
  # We have this check because earlier Sources held their runtime params so
  # a common problem is Sources that haven't removed theirs yet.
  for field in source_fields:
    if field.name == 'runtime_params':
      raise ValueError(
          'Source dataclasses must not have a `runtime_params` '
          f'field but {source_type} does.'
      )

  # Filter out fields that shouldn't be passed to constructor
  source_fields = [f for f in source_fields if f.init]

  if links_back:
    assert sum([f.name == 'source_models' for f in source_fields]) == 1
    source_fields = [f for f in source_fields if f.name != 'source_models']

  name_type_field_tuples = [
      (field.name, field.type, field) for field in source_fields
  ]

  runtime_params_ntf = (
      'runtime_params',
      runtime_params_type,
      dataclasses.field(default_factory=runtime_params_type),
  )

  new_field_ntfs = [runtime_params_ntf]
  builder_ntfs = name_type_field_tuples + new_field_ntfs
  builder_type_name = source_type.__name__ + 'Builder'

  def check_kwargs(source_init_kwargs, context_msg):
    for f in source_fields:
      v = source_init_kwargs[f.name]
      if isinstance(f.type, str):
        if f.type == 'tuple[AffectedCoreProfile, ...]':
          assert isinstance(v, tuple)
          assert all([isinstance(var, AffectedCoreProfile) for var in v])
        elif f.type == 'tuple[runtime_params_lib.Mode, ...]':
          assert isinstance(v, tuple)
          assert all([isinstance(var, runtime_params_lib.Mode) for var in v])
        elif f.type == 'SourceProfileFunction | None':
          assert v is None or callable(v)
        elif f.type == 'source.SourceProfileFunction':
          if not callable(v):
            raise TypeError(
                f'While {context_msg} {source_type} got field '
                f'{f.name} of type source.SoureProfileFunction '
                ' but was passed constructor argument with value '
                f'{v} of type {type(v)}. It is not callable, so '
                'it cannot be a SourceProfileFunction.'
            )
        elif f.type in [
            'source.SourceOutputShapeFunction',
            'SourceOutputShapeFunction',
        ]:
          if not callable(v):
            raise TypeError(
                f'While {context_msg} {source_type} got field '
                f'{f.name} of type source.SoureProfileFunction '
                ' but was passed constructor argument with value '
                f'{v} of type {type(v)}. It is not callable, so '
                'it cannot be a SourceProfileFunction.'
            )
        else:
          raise TypeError(f'Unrecognized type string: {f.type}')
      else:
        try:
          type_works = isinstance(v, f.type)
        except TypeError as exc:
          raise TypeError(
              f'While {context_msg} {source_type} got field '
              f'{f.name} whose type is {f.type} of type'
              f'{type(f.type)}. This is not a valid type.'
          ) from exc
        if not type_works:
          raise TypeError(
              f'While {context_msg} {source_type} got argument '
              f'{f.name} of type {type(v)} but expected '
              f'{f.type}).'
          )

  # pylint doesn't like this function name because it doesn't realize
  # this function is to be installed in a class
  def __post_init__(self):  # pylint:disable=invalid-name
    source_init_kwargs = _convert_source_builder_to_init_kwargs(self)
    check_kwargs(source_init_kwargs, 'making builder')
    # check_kwargs checks only the kwargs to Source, not SourceBuilder,
    # so it doesn't check "runtime_params"
    runtime_params = self.runtime_params
    if not isinstance(runtime_params, runtime_params_type):
      raise TypeError(
          f'Expected {runtime_params_type}, got {type(runtime_params)}'
      )

  if links_back:

    def build_source(self, source_models):
      source_init_kwargs = _convert_source_builder_to_init_kwargs(self)
      source_init_kwargs['source_models'] = source_models
      check_kwargs(source_init_kwargs, 'building')
      return source_type(**source_init_kwargs)

  else:

    def build_source(self):
      source_init_kwargs = _convert_source_builder_to_init_kwargs(self)
      check_kwargs(source_init_kwargs, 'building')
      return source_type(**source_init_kwargs)

  return dataclasses.make_dataclass(
      builder_type_name,
      builder_ntfs,
      namespace={
          '__call__': build_source,
          'links_back': links_back,
          '__post_init__': __post_init__,
      },
      frozen=False,  # One role of the Builder class is to hold
      # the mutable runtime params
      kw_only=True,
  )


SourceBuilder = make_source_builder(Source)
SingleProfileSourceBuilder = make_source_builder(SingleProfileSource)
SingleProfilePsiSourceBuilder = make_source_builder(SingleProfilePsiSource)
SingleProfileNeSourceBuilder = make_source_builder(SingleProfileNeSource)
SingleProfileTempIonSourceBuilder = make_source_builder(
    SingleProfileTempIonSource
)
SingleProfileTempElSourceBuilder = make_source_builder(
    SingleProfileTempElSource
)
IonElectronSourceBuilder = make_source_builder(IonElectronSource)
