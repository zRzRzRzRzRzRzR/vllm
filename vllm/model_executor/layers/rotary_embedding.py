# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

# Adapted from
# https://github.com/huggingface/transformers/blob/v4.33.2/src/transformers/models/llama/modeling_llama.py
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
"""Rotary Positional Embeddings."""
import itertools
import math
from typing import Any, Optional, Union

import numpy as np
import torch
import torch.nn as nn
from transformers import PretrainedConfig

from vllm.model_executor.custom_op import CustomOp
from vllm.platforms import current_platform

if current_platform.is_cuda():
    from vllm.vllm_flash_attn.layers.rotary import apply_rotary_emb


def _rotate_neox(x: torch.Tensor) -> torch.Tensor:
    x1 = x[..., :x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2:]
    return torch.cat((-x2, x1), dim=-1)


def _rotate_gptj(x: torch.Tensor) -> torch.Tensor:
    x1 = x[..., ::2]
    x2 = x[..., 1::2]
    x = torch.stack((-x2, x1), dim=-1)
    return x.flatten(-2)


def _apply_rotary_emb_torch(
    x: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    is_neox_style: bool,
) -> torch.Tensor:
    cos = cos.unsqueeze(-2).to(x.dtype)
    sin = sin.unsqueeze(-2).to(x.dtype)
    if is_neox_style:
        x1, x2 = torch.chunk(x, 2, dim=-1)
    else:
        x1 = x[..., ::2]
        x2 = x[..., 1::2]
    o1 = x1 * cos - x2 * sin
    o2 = x2 * cos + x1 * sin
    if is_neox_style:
        return torch.cat((o1, o2), dim=-1)
    else:
        return torch.stack((o1, o2), dim=-1).flatten(-2)


def _apply_rotary_emb(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor,
                      is_neox_style: bool) -> torch.Tensor:
    """
    Args:
        x: [num_tokens, num_heads, head_size]
        cos: [num_tokens, head_size // 2]
        sin: [num_tokens, head_size // 2]
        is_neox_style: Whether to use the Neox-style or GPT-J-style rotary
            positional embeddings.
    """
    if current_platform.is_cuda():
        return apply_rotary_emb(x.unsqueeze(0), cos, sin,
                                not is_neox_style).squeeze(0)
    else:
        return _apply_rotary_emb_torch(x, cos, sin, is_neox_style)


@CustomOp.register("rotary_embedding")
class RotaryEmbedding(CustomOp):
    """Original rotary positional embedding."""

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
    ) -> None:
        super().__init__()
        self.head_size = head_size
        self.rotary_dim = rotary_dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        self.is_neox_style = is_neox_style
        self.dtype = dtype

        cache = self._compute_cos_sin_cache()
        cache = cache.to(dtype)
        self.cos_sin_cache: torch.Tensor
        self.register_buffer("cos_sin_cache", cache, persistent=False)

    def _compute_inv_freq(self, base: float) -> torch.Tensor:
        """Compute the inverse frequency."""
        # NOTE(woosuk): To exactly match the HF implementation, we need to
        # use CPU to compute the cache and then move it to GPU. However, we
        # create the cache on GPU for faster initialization. This may cause
        # a slight numerical difference between the HF implementation and ours.
        inv_freq = 1.0 / (base**(torch.arange(
            0, self.rotary_dim, 2, dtype=torch.float) / self.rotary_dim))
        return inv_freq

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        """Compute the cos and sin cache."""
        inv_freq = self._compute_inv_freq(self.base)
        t = torch.arange(self.max_position_embeddings, dtype=torch.float)

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache

    def forward_native(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        """A PyTorch-native implementation of forward()."""
        if offsets is not None:
            positions = positions + offsets
        positions = positions.flatten()
        num_tokens = positions.shape[0]
        cos_sin = self.cos_sin_cache.index_select(0, positions)
        cos, sin = cos_sin.chunk(2, dim=-1)

        query_shape = query.shape
        query = query.view(num_tokens, -1, self.head_size)
        query_rot = query[..., :self.rotary_dim]
        query_pass = query[..., self.rotary_dim:]
        query_rot = _apply_rotary_emb_torch(query_rot, cos, sin,
                                            self.is_neox_style)
        query = torch.cat((query_rot, query_pass), dim=-1).reshape(query_shape)

        # key may be None in some cases, e.g. cross-layer KV sharing
        if key is not None:
            key_shape = key.shape
            key = key.view(num_tokens, -1, self.head_size)
            key_rot = key[..., :self.rotary_dim]
            key_pass = key[..., self.rotary_dim:]
            key_rot = _apply_rotary_emb_torch(key_rot, cos, sin,
                                              self.is_neox_style)
            key = torch.cat((key_rot, key_pass), dim=-1).reshape(key_shape)
        return query, key

    def forward_cuda(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        from vllm import _custom_ops as ops

        # __setattr__ in nn.Module (called by `self.cos_sin_cache = ...`)
        # is expensive, so avoid calling it if possible
        if self.cos_sin_cache.device != query.device or \
            self.cos_sin_cache.dtype != query.dtype:
            self.cos_sin_cache = self.cos_sin_cache.to(query.device,
                                                       dtype=query.dtype)

        # ops.rotary_embedding()/batched_rotary_embedding()
        # are in-place operations that update the query and key tensors.
        if offsets is not None:
            ops.batched_rotary_embedding(positions, query, key, self.head_size,
                                         self.cos_sin_cache,
                                         self.is_neox_style, self.rotary_dim,
                                         offsets)
        else:
            ops.rotary_embedding(positions, query, key, self.head_size,
                                 self.cos_sin_cache, self.is_neox_style)
        return query, key

    def forward_xpu(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        from vllm._ipex_ops import ipex_ops as ops

        self.cos_sin_cache = self.cos_sin_cache.to(positions.device,
                                                   dtype=query.dtype)
        # ops.rotary_embedding()/batched_rotary_embedding()
        # are in-place operations that update the query and key tensors.
        if key is None:
            # XPU kernel doesn't support key=None so fall back to native impl
            # TODO(sarckk): add support for optional key in
            # ipex.llm.functional.rotary_embedding_batched
            return self.forward_native(positions, query, key, offsets)
        else:
            if offsets is not None:
                ops.batched_rotary_embedding(positions, query, key,
                                             self.head_size,
                                             self.cos_sin_cache,
                                             self.is_neox_style,
                                             self.rotary_dim, offsets)
            else:
                ops.rotary_embedding(positions, query, key, self.head_size,
                                     self.cos_sin_cache, self.is_neox_style)
        return query, key

    def forward_neuron(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:

        def _apply_rotary_emb_neuron(
            x: torch.Tensor,
            cos: torch.Tensor,
            sin: torch.Tensor,
            is_neox_style: bool,
        ) -> torch.Tensor:
            cos = cos.unsqueeze(-2).to(x.dtype)
            sin = sin.unsqueeze(-2).to(x.dtype)
            if is_neox_style:
                x1, x2 = torch.chunk(x, 2, dim=-1)
            else:
                # x1 = x[..., ::2]

                # x2 = x[..., 1::2]
                d = x.shape[-1] // 2
                x_reshaped = x.view(-1, x.shape[-1])
                x1 = x_reshaped[:, ::2].view(*x.shape[:-1], d)
                x2 = x_reshaped[:, 1::2].view(*x.shape[:-1], d)
            o1 = x1 * cos - x2 * sin
            o2 = x2 * cos + x1 * sin
            if is_neox_style:
                return torch.cat((o1, o2), dim=-1)
            else:
                return torch.stack((o1, o2), dim=-1).flatten(-2)

        if offsets is not None:
            positions = positions + offsets

        self.cos_sin_cache = self.cos_sin_cache.to(query.device,
                                                   dtype=query.dtype)

        positions = positions.flatten()
        num_tokens = positions.shape[0]
        cos_sin = self.cos_sin_cache.index_select(0, positions)
        cos, sin = cos_sin.chunk(2, dim=-1)

        query_shape = query.shape
        query = query.view(num_tokens, -1, self.head_size)
        if key is not None:
            key_shape = key.shape
            key = key.view(num_tokens, -1, self.head_size)

        if self.rotary_dim == self.head_size:
            query = _apply_rotary_emb(query, cos, sin, self.is_neox_style)
            query = query.reshape(query_shape)
            if key is not None:
                key = _apply_rotary_emb(key, cos, sin, self.is_neox_style)
                key = key.reshape(key_shape)
        else:
            head_size = query.shape[-1]
            query_reshaped = query.view(-1, head_size)
            query_pass = query_reshaped[:, self.rotary_dim:].view(
                *query.shape[:-1], head_size - self.rotary_dim)
            query_rot = query_reshaped[:, :self.rotary_dim].view(
                *query.shape[:-1], self.rotary_dim)
            query_rot = _apply_rotary_emb_neuron(query_rot, cos, sin,
                                                 self.is_neox_style)
            query = torch.cat((query_rot, query_pass),
                              dim=-1).reshape(query_shape)

            if key is not None:
                key_reshaped = key.view(-1, head_size)
                key_pass = key_reshaped[:, self.rotary_dim:].view(
                    *key.shape[:-1], head_size - self.rotary_dim)
                key_rot = key_reshaped[:, :self.rotary_dim].view(
                    *key.shape[:-1], self.rotary_dim)
                key_rot = _apply_rotary_emb_neuron(key_rot, cos, sin,
                                                   self.is_neox_style)
                key = torch.cat((key_rot, key_pass), dim=-1).reshape(key_shape)
        return query, key

    def extra_repr(self) -> str:
        s = f"head_size={self.head_size}, rotary_dim={self.rotary_dim}"
        s += f", max_position_embeddings={self.max_position_embeddings}"
        s += f", base={self.base}, is_neox_style={self.is_neox_style}"
        return s


class LinearScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with linear scaling.

    It supports multiple scaling factors. Since multiple LoRA adapters may have
    different scaling factors, we need multiple cos/sin caches. In this way,
    instead of running rotary embedding kernel per lora, we can run multiple
    lora in a batched way.

    In addition to that, we also keep the cos/sin cache for the scaling factor
    of 1 (default) at all times.

    Exemplary for two scaling factors x=1, y and z with embeddings
    [[x11, x12, ... x1m], ..., [xn1, xn2, ..., xnm]] and
    [[y11, y12, ... y1o], ..., [yn1, yn2, ..., yno]], and
    [[z11, z12, ... z1p], ..., [zn1, zn2, ..., znp]],

    we construct the cos/sin cache as follows:
    [[x11, x12, ... x1m, y11, y12, ... y1o, z11, z12, ... z1p],
        ...
     [xn1, xn2, ... xnm, yn1, yn2, ... yno, zn1, zn2, ... znp]]

    We then use offsets to index into the cos/sin cache for
    the respective scaling factors.

    The offset to cache can be accessed via `scaling_factor_to_offset` API.

    Credits to the Reddit user /u/kaiokendev
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        scaling_factors: Union[list[float], float],
        dtype: torch.dtype,
    ) -> None:
        if isinstance(scaling_factors, float):
            scaling_factors = [scaling_factors]
        self.scaling_factors: list[float] = scaling_factors  # noqa
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)
        # Lazy initialized.
        self._scaling_factor_to_offset: dict[float, int]

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(self.base)
        cache_list: list[torch.Tensor] = []
        # offsets to the next cache in a tensor.
        # Each offset corresponds to the same index in scaling_factors.
        offsets: list[int] = []
        for scaling_factor in self.scaling_factors:
            # NOTE(woosuk): self.max_position_embeddings is the original
            # maximum length before applying the rope scaling.
            # Thus, the maximum length after applying the rope scaling is
            # self.max_position_embeddings * self.scaling_factor.
            max_len = self.max_position_embeddings * scaling_factor
            t = torch.arange(max_len, dtype=torch.float)
            t = t / scaling_factor

            freqs = torch.einsum("i,j -> ij", t, inv_freq)
            cos = freqs.cos()
            sin = freqs.sin()
            cache = torch.cat((cos, sin), dim=-1)
            if not cache_list:
                offset = 0
            else:
                last_offset = offsets[-1]
                next_max_len = cache_list[-1].shape[0]
                offset = last_offset + next_max_len
            offsets.append(offset)
            cache_list.append(cache)
        self._scaling_factor_to_offset = {
            float(scaling_factor): offsets[i]
            for i, scaling_factor in enumerate(self.scaling_factors)
        }
        assert len(self.scaling_factors) == len(offsets)
        return torch.cat(cache_list, dim=0)

    @property
    def scaling_factor_to_offset(self) -> dict[float, int]:
        return self._scaling_factor_to_offset


class NTKScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with fixed and mixed NTK scaling.
    https://kexue.fm/archives/9706 """

    def __init__(self,
                 head_size: int,
                 rotary_dim: int,
                 max_position_embeddings: int,
                 base: float,
                 is_neox_style: bool,
                 scaling_factor: float,
                 dtype: torch.dtype,
                 mixed_b: Optional[float] = None) -> None:
        self.scaling_factor = scaling_factor
        self.mixed_b = mixed_b
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_inv_freq(self, base: float) -> torch.Tensor:
        base = self.base * (self.scaling_factor if self.mixed_b is None else 1)
        inv_freq = super()._compute_inv_freq(base)

        if self.mixed_b is None:
            inv_freq = inv_freq / self.scaling_factor**(2 / self.rotary_dim)
        else:
            a = torch.tensor(self.scaling_factor).log() / (self.rotary_dim /
                                                           2)**self.mixed_b
            lambda_1_m = (a * torch.arange(
                1, self.rotary_dim // 2 + 1).float()**self.mixed_b).exp()
            inv_freq = inv_freq / lambda_1_m

        return inv_freq


class DynamicNTKScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with Dynamic NTK scaling.

    Credits to the Reddit users /u/bloc97 and /u/emozilla
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        scaling_factor: float,
        dtype: torch.dtype,
    ) -> None:
        self.scaling_factor = scaling_factor
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        # NOTE(woosuk): self.max_position_embeddings is the original
        # maximum length before applying the rope scaling.
        # Thus, the maximum length after applying the rope scaling is
        # self.max_position_embeddings * self.scaling_factor.
        max_len = self.max_position_embeddings * self.scaling_factor
        base = self.base * (
            (self.scaling_factor * max_len / self.max_position_embeddings) -
            (self.scaling_factor - 1))**(self.rotary_dim /
                                         (self.rotary_dim - 2))
        inv_freq = self._compute_inv_freq(base)
        t = torch.arange(max_len, dtype=torch.float)

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache


class DynamicNTKAlphaRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with Dynamic NTK alpha.

    Based on the original RotaryEmbedding implementation.
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        scaling_alpha: float,
        dtype: torch.dtype,
    ) -> None:
        self.scaling_alpha = scaling_alpha
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        # For Hunyuan DynamicNTKAlphaRotaryEmbedding
        max_len = self.max_position_embeddings
        base = self.base * self.scaling_alpha**(self.rotary_dim /
                                                (self.rotary_dim - 2))
        inv_freq = self._compute_inv_freq(base)
        t = torch.arange(max_len, dtype=torch.float)

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache


# Inverse dim formula to find dim based on number of rotations
def _yarn_find_correction_dim(num_rotations: int,
                              dim: int,
                              base: float = 10000,
                              max_position_embeddings: int = 2048) -> float:
    return (dim * math.log(max_position_embeddings /
                           (num_rotations * 2 * math.pi))) / (2 *
                                                              math.log(base))


# Find dim range bounds based on rotations
def _yarn_find_correction_range(
        low_rot: int,
        high_rot: int,
        dim: int,
        base: float = 10000,
        max_position_embeddings: int = 2048) -> tuple[int, int]:
    low = math.floor(
        _yarn_find_correction_dim(low_rot, dim, base, max_position_embeddings))
    high = math.ceil(
        _yarn_find_correction_dim(high_rot, dim, base,
                                  max_position_embeddings))
    return max(low, 0), min(high, dim - 1)  # Clamp values just in case


def _yarn_linear_ramp_mask(low: float, high: float, dim: int,
                           dtype: torch.dtype) -> torch.Tensor:
    if low == high:
        high += 0.001  # Prevent singularity

    linear_func = (torch.arange(dim, dtype=dtype) - low) / (high - low)
    ramp_func = torch.clamp(linear_func, 0, 1)
    return ramp_func


def _yarn_get_mscale(scale: float = 1) -> float:
    if scale <= 1:
        return 1.0
    return 0.1 * math.log(scale) + 1.0


class YaRNScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with YaRN method.

    Credits to Peng et al. github.com/jquesnelle/yarn
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        scaling_factor: float,
        dtype: torch.dtype,
        *,
        extrapolation_factor: float = 1,
        attn_factor: float = 1,
        beta_fast: int = 32,
        beta_slow: int = 1,
    ) -> None:
        self.scaling_factor = scaling_factor
        self.extrapolation_factor = extrapolation_factor
        self.attn_factor = attn_factor
        self.beta_fast = beta_fast
        self.beta_slow = beta_slow
        # Get n-d magnitude scaling corrected for interpolation
        self.mscale = float(
            _yarn_get_mscale(self.scaling_factor) * attn_factor)
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_inv_freq(self, scaling_factor: float) -> torch.Tensor:
        pos_freqs = self.base**(
            torch.arange(0, self.rotary_dim, 2, dtype=torch.float) /
            self.rotary_dim)
        inv_freq_extrapolation = 1.0 / pos_freqs
        inv_freq_interpolation = 1.0 / (scaling_factor * pos_freqs)

        low, high = _yarn_find_correction_range(self.beta_fast, self.beta_slow,
                                                self.rotary_dim, self.base,
                                                self.max_position_embeddings)
        # Get n-d rotational scaling corrected for extrapolation
        inv_freq_mask = (1 - _yarn_linear_ramp_mask(
            low, high, self.rotary_dim // 2,
            dtype=torch.float)) * self.extrapolation_factor
        inv_freq = inv_freq_interpolation * (
            1 - inv_freq_mask) + inv_freq_extrapolation * inv_freq_mask
        return inv_freq

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(self.scaling_factor)
        t = torch.arange(self.max_position_embeddings * self.scaling_factor,
                         dtype=torch.float32)
        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = (freqs.cos() * self.mscale)
        sin = (freqs.sin() * self.mscale)
        cache = torch.cat((cos, sin), dim=-1)
        return cache


class Phi3LongRoPEScaledRotaryEmbedding(nn.Module):
    """Phi3 family of models scaled rotary embedding.

    Based on the original RotaryEmbedding implementation.
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        original_max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
        short_factor: list[float],
        long_factor: list[float],
        short_mscale: Optional[float] = None,
        long_mscale: Optional[float] = None,
    ):
        super().__init__()

        if is_neox_style is False:
            raise ValueError(
                "`Phi3LongRoPEScaledRotaryEmbedding` only supports neox_style."
            )

        self.rotary_dim = rotary_dim
        self.head_size = head_size
        self.max_position_embeddings = max_position_embeddings
        self.original_max_position_embeddings = original_max_position_embeddings
        self.base = base
        self.short_factor = short_factor
        self.long_factor = long_factor

        scale = self.max_position_embeddings / \
                self.original_max_position_embeddings
        if scale <= 1.0:
            scaling_factor = 1.0
        else:
            scaling_factor = math.sqrt(
                1 + math.log(scale) /
                math.log(self.original_max_position_embeddings))
        if short_mscale is None:
            short_mscale = scaling_factor
        if long_mscale is None:
            long_mscale = scaling_factor

        self.short_mscale = short_mscale
        self.long_mscale = long_mscale

        short_cache = self._compute_cos_sin_cache(
            original_max_position_embeddings, short_factor, short_mscale)
        short_cache = short_cache.to(dtype)

        long_cache = self._compute_cos_sin_cache(max_position_embeddings,
                                                 long_factor, long_mscale)
        long_cache = long_cache.to(dtype)

        long_short_cache = torch.cat([short_cache, long_cache], dim=0)
        self.register_buffer("long_short_cos_sin_cache",
                             long_short_cache,
                             persistent=False)

    def _compute_inv_freq(self, rescale_factors: list[float]) -> torch.Tensor:
        rescale_factors = torch.tensor(rescale_factors, dtype=torch.float32)
        inv_freq = 1.0 / (rescale_factors * (self.base**(torch.arange(
            0, self.rotary_dim, 2, dtype=torch.float) / self.rotary_dim)))
        return inv_freq

    def _compute_cos_sin_cache(
        self,
        max_position_embeddings: int,
        rescale_factors: list[float],
        mscale: float,
    ) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(rescale_factors)
        t = torch.arange(max_position_embeddings, dtype=torch.float)
        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos() * mscale
        sin = freqs.sin() * mscale
        cache = torch.cat((cos, sin), dim=-1)
        return cache

    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        assert key is not None
        query = query.view(*query.shape[:-1], -1, self.head_size)
        key = key.view(*key.shape[:-1], -1, self.head_size)

        k = self.original_max_position_embeddings
        long_prompt_offset = (torch.any(positions > k).float() *
                              torch.full_like(positions, k)).long()
        idx = (torch.add(positions, long_prompt_offset)
               if long_prompt_offset is not None else positions)
        idx = torch.add(idx, offsets) if offsets is not None else idx
        cos_sin = torch.index_select(self.long_short_cos_sin_cache, 0, idx)

        cos, sin = cos_sin.chunk(2, dim=-1)
        cos = cos.repeat(1, 2).unsqueeze(-2)
        sin = sin.repeat(1, 2).unsqueeze(-2)

        query_rot = query[..., :self.rotary_dim]
        query_pass = query[..., self.rotary_dim:]
        query_rot = query_rot * cos + _rotate_neox(query_rot) * sin
        query = torch.cat((query_rot, query_pass), dim=-1)

        key_rot = key[..., :self.rotary_dim]
        key_pass = key[..., self.rotary_dim:]
        key_rot = key_rot * cos + _rotate_neox(key_rot) * sin
        key = torch.cat((key_rot, key_pass), dim=-1)

        return query.flatten(-2), key.flatten(-2)


def yarn_get_mscale(scale: float = 1, mscale: float = 1) -> float:
    if scale <= 1:
        return 1.0
    return 0.1 * mscale * math.log(scale) + 1.0


class DeepseekScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with YaRN method.

    Credits to Peng et al. github.com/jquesnelle/yarn
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        scaling_factor: float,
        dtype: torch.dtype,
        *,
        extrapolation_factor: float = 1,
        attn_factor: float = 1,
        beta_fast: int = 32,
        beta_slow: int = 1,
        mscale: float = 1,
        mscale_all_dim: float = 0,
    ) -> None:
        self.scaling_factor = scaling_factor
        self.extrapolation_factor = extrapolation_factor
        self.attn_factor = attn_factor
        self.beta_fast = beta_fast
        self.beta_slow = beta_slow
        # Get n-d magnitude scaling corrected for interpolation.
        self.mscale = float(
            yarn_get_mscale(self.scaling_factor, float(mscale)) /
            yarn_get_mscale(self.scaling_factor, float(mscale_all_dim)) *
            attn_factor)
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_inv_freq(self, scaling_factor: float) -> torch.Tensor:
        pos_freqs = self.base**(
            torch.arange(0,
                         self.rotary_dim,
                         2,
                         dtype=torch.float,
                         device=current_platform.device_type) /
            self.rotary_dim)
        inv_freq_extrapolation = 1.0 / pos_freqs
        inv_freq_interpolation = 1.0 / (scaling_factor * pos_freqs)

        low, high = _yarn_find_correction_range(self.beta_fast, self.beta_slow,
                                                self.rotary_dim, self.base,
                                                self.max_position_embeddings)
        # Get n-d rotational scaling corrected for extrapolation
        inv_freq_mask = (1 - _yarn_linear_ramp_mask(
            low, high, self.rotary_dim // 2,
            dtype=torch.float)) * self.extrapolation_factor
        inv_freq = inv_freq_interpolation * (
            1 - inv_freq_mask) + inv_freq_extrapolation * inv_freq_mask
        return inv_freq

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(self.scaling_factor)
        t = torch.arange(self.max_position_embeddings * self.scaling_factor,
                         device=current_platform.device_type,
                         dtype=torch.float32)
        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = (freqs.cos() * self.mscale)
        sin = (freqs.sin() * self.mscale)
        cache = torch.cat((cos, sin), dim=-1)
        return cache

    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        """PyTorch-native implementation equivalent to forward()."""
        assert key is not None
        query_rot = query[..., :self.rotary_dim]
        key_rot = key[..., :self.rotary_dim]
        if self.rotary_dim < self.head_size:
            query_pass = query[..., self.rotary_dim:]
            key_pass = key[..., self.rotary_dim:]

        if self.cos_sin_cache.device != positions.device:
            self.cos_sin_cache: torch.Tensor = self.cos_sin_cache.to(
                positions.device)
        cos_sin = self.cos_sin_cache[torch.add(positions, offsets)
                                     if offsets is not None else positions]
        cos, sin = cos_sin.chunk(2, dim=-1)
        if self.is_neox_style:
            # NOTE(woosuk): Here we assume that the positions tensor has the
            # shape [batch_size, seq_len].
            cos = cos.repeat(1, 1, 2).unsqueeze(-2)
            sin = sin.repeat(1, 1, 2).unsqueeze(-2)
        else:
            cos = cos.repeat_interleave(2, dim=-1).unsqueeze(-2)
            sin = sin.repeat_interleave(2, dim=-1).unsqueeze(-2)

        rotate_fn = _rotate_neox if self.is_neox_style else _rotate_gptj
        query_rot = query_rot * cos + rotate_fn(query_rot) * sin
        key_rot = key_rot * cos + rotate_fn(key_rot) * sin

        if self.rotary_dim < self.head_size:
            query = torch.cat((query_rot, query_pass), dim=-1)
            key = torch.cat((key_rot, key_pass), dim=-1)
        else:
            query = query_rot
            key = key_rot
        return query, key


class Llama3RotaryEmbedding(RotaryEmbedding):

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
        scaling_factor: float,
        low_freq_factor: float,
        high_freq_factor: float,
        orig_max_position: int,
    ) -> None:
        self.scaling_factor = scaling_factor
        self.low_freq_factor = low_freq_factor
        self.high_freq_factor = high_freq_factor
        self.orig_max_position = orig_max_position
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_inv_freq(self, base: float) -> torch.Tensor:
        inv_freqs = super()._compute_inv_freq(base)
        low_freq_wavelen = self.orig_max_position / self.low_freq_factor
        high_freq_wavelen = self.orig_max_position / self.high_freq_factor

        wave_len = 2 * math.pi / inv_freqs
        if self.low_freq_factor != self.high_freq_factor:
            smooth = (self.orig_max_position / wave_len - self.low_freq_factor
                      ) / (self.high_freq_factor - self.low_freq_factor)
        else:
            smooth = 0
        new_freqs = torch.where(
            wave_len < high_freq_wavelen,
            inv_freqs,
            torch.where(
                wave_len > low_freq_wavelen,
                inv_freqs / self.scaling_factor,
                (1 - smooth) * inv_freqs / self.scaling_factor +
                smooth * inv_freqs,
            ),
        )
        return new_freqs


class Llama4VisionRotaryEmbedding(RotaryEmbedding):

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
    ):
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style, dtype)

    def _compute_inv_freq(self, base: float) -> torch.Tensor:
        inv_freqs = super()._compute_inv_freq(base)
        inv_freqs = inv_freqs[:(self.rotary_dim // 2)]
        return inv_freqs

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(self.base)

        # self.max_position_embeddings here is number of image patches
        # i.e. (image_size // patch_size) ** 2
        num_patches = self.max_position_embeddings
        img_idx = torch.arange(num_patches,
                    dtype=torch.int32) \
                    .reshape(num_patches, 1)
        img_idx = torch.cat([img_idx, img_idx[:1]], dim=0)
        img_idx[-1, -1] = -2  # set to ID_CLS_TOKEN
        num_patches_single_dim = int(math.sqrt(num_patches))
        frequencies_x = img_idx % num_patches_single_dim
        frequencies_y = img_idx // num_patches_single_dim
        freqs_x = ((frequencies_x + 1)[..., None] *
                   inv_freq[None, None, :]).repeat_interleave(2, dim=-1)
        freqs_y = ((frequencies_y + 1)[..., None] *
                   inv_freq[None, None, :]).repeat_interleave(2, dim=-1)
        freqs = torch.cat([freqs_x, freqs_y],
                          dim=-1).float().contiguous()[..., ::2]
        freqs = freqs.masked_fill(img_idx.reshape(-1, 1, 1) < 0, 0)
        cache = torch.view_as_complex(
            torch.stack([torch.cos(freqs), torch.sin(freqs)], dim=-1))
        return cache

    def forward(
        self,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        assert key is not None
        self.cos_sin_cache: torch.Tensor = self.cos_sin_cache.to(query.device)
        query_ = torch.view_as_complex(query.float().reshape(
            *query.shape[:-1], -1, 2))
        key_ = torch.view_as_complex(key.float().reshape(
            *key.shape[:-1], -1, 2))
        broadcast_shape = [
            d if i == 1 or i == (query_.ndim - 1) else 1
            for i, d in enumerate(query_.shape)
        ]
        freqs_ci = self.cos_sin_cache.view(*broadcast_shape)
        query_out = torch.view_as_real(query_ * freqs_ci).flatten(3)
        key_out = torch.view_as_real(key_ * freqs_ci).flatten(3)
        return query_out.type_as(query), key_out.type_as(key)


class MRotaryEmbedding(RotaryEmbedding):
    """Rotary Embedding with Multimodal Sections."""

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
        mrope_section: Optional[list[int]] = None,
    ) -> None:
        # In Qwen2.5-VL, the maximum index value is related to the duration of
        # the input video. We enlarge max_position_embeddings to 4 times to get
        # a larger the cos and sin cache.
        self.cache_max_position_num = max_position_embeddings * 4
        super().__init__(head_size, rotary_dim, self.cache_max_position_num,
                         base, is_neox_style, dtype)

        self.mrope_section = mrope_section
        if self.mrope_section:
            assert sum(self.mrope_section) == rotary_dim // 2

    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        """PyTorch-native implementation equivalent to forward().

        Args:
            positions:
                [num_tokens,] (text only) or
                [3, num_tokens] (T/H/W positions with multimodal inputs)
            query: [num_tokens, num_heads * head_size]
            key: [num_tokens, num_kv_heads * head_size]
        """
        assert positions.ndim == 1 or positions.ndim == 2
        assert key is not None

        num_tokens = positions.shape[-1]
        cos_sin = self.cos_sin_cache[positions]
        cos, sin = cos_sin.chunk(2, dim=-1)
        if positions.ndim == 2:
            assert self.mrope_section

            cos = torch.cat([
                m[i]
                for i, m in enumerate(cos.split(self.mrope_section, dim=-1))
            ],
                            dim=-1)
            sin = torch.cat([
                m[i]
                for i, m in enumerate(sin.split(self.mrope_section, dim=-1))
            ],
                            dim=-1)

        query_shape = query.shape
        query = query.view(num_tokens, -1, self.head_size)
        query_rot = query[..., :self.rotary_dim]
        query_pass = query[..., self.rotary_dim:]
        query_rot = _apply_rotary_emb(query_rot, cos, sin, self.is_neox_style)
        query = torch.cat((query_rot, query_pass), dim=-1).reshape(query_shape)

        key_shape = key.shape
        key = key.view(num_tokens, -1, self.head_size)
        key_rot = key[..., :self.rotary_dim]
        key_pass = key[..., self.rotary_dim:]
        key_rot = _apply_rotary_emb(key_rot, cos, sin, self.is_neox_style)
        key = torch.cat((key_rot, key_pass), dim=-1).reshape(key_shape)
        return query, key

    @classmethod
    def get_input_positions(
        cls,
        input_tokens: list[int],
        hf_config: PretrainedConfig,
        image_grid_thw: Optional[Union[list[list[int]], torch.Tensor]],
        video_grid_thw: Optional[Union[list[list[int]], torch.Tensor]],
        second_per_grid_ts: Optional[list[float]],
        context_len: int = 0,
        seq_len: Optional[int] = None,
        audio_feature_lengths: Optional[torch.Tensor] = None,
        use_audio_in_video: bool = False,
    ) -> tuple[list[list[int]], int]:
        """Get mrope input positions and delta value."""

        image_grid_thw = [] if image_grid_thw is None else image_grid_thw
        video_grid_thw = [] if video_grid_thw is None else video_grid_thw
        second_per_grid_ts = [] if second_per_grid_ts is None else \
            second_per_grid_ts

        llm_positions, mrope_position_delta = \
            cls.get_input_positions_tensor(
                input_tokens=input_tokens,
                hf_config=hf_config,
                image_grid_thw=image_grid_thw,
                video_grid_thw=video_grid_thw,
                second_per_grid_ts=second_per_grid_ts,
                context_len=context_len,
                seq_len=seq_len,
                audio_feature_lengths=audio_feature_lengths,
                use_audio_in_video=use_audio_in_video,
            )

        return llm_positions.tolist(), mrope_position_delta

    @classmethod
    def get_input_positions_tensor(
        cls,
        input_tokens: list[int],
        hf_config: PretrainedConfig,
        image_grid_thw: Union[list[list[int]], torch.Tensor],
        video_grid_thw: Union[list[list[int]], torch.Tensor],
        second_per_grid_ts: list[float],
        context_len: int = 0,
        seq_len: Optional[int] = None,
        audio_feature_lengths: Optional[torch.Tensor] = None,
        use_audio_in_video: bool = False,
    ) -> tuple[torch.Tensor, int]:
        from vllm.transformers_utils.config import thinker_uses_mrope
        if thinker_uses_mrope(hf_config):
            return cls._omni_get_input_positions_tensor(
                input_tokens=input_tokens,
                hf_config=hf_config,
                image_grid_thw=image_grid_thw,
                video_grid_thw=video_grid_thw,
                second_per_grid_ts=second_per_grid_ts,
                context_len=context_len,
                seq_len=seq_len,
                audio_feature_lengths=audio_feature_lengths,
                use_audio_in_video=use_audio_in_video,
            )
        elif hf_config.model_type in ["glm4v", "glm4v_moe"]:
            return cls._glm4v_get_input_positions_tensor(
                input_tokens=input_tokens,
                hf_config=hf_config,
                image_grid_thw=image_grid_thw,
                video_grid_thw=video_grid_thw,
                context_len=context_len,
                seq_len=seq_len,
            )
        else:
            return cls._vl_get_input_positions_tensor(
                input_tokens=input_tokens,
                hf_config=hf_config,
                image_grid_thw=image_grid_thw,
                video_grid_thw=video_grid_thw,
                second_per_grid_ts=second_per_grid_ts,
                context_len=context_len,
                seq_len=seq_len,
            )

    @classmethod
    def _glm4v_get_input_positions_tensor(
        cls,
        input_tokens: list[int],
        hf_config: PretrainedConfig,
        image_grid_thw: Union[list[list[int]], torch.Tensor],
        video_grid_thw: Union[list[list[int]], torch.Tensor],
        context_len: int = 0,
        seq_len: Optional[int] = None,
    ) -> tuple[torch.Tensor, int]:
        """Get mrope input positions and delta value for GLM4V."""

        image_token_id = hf_config.image_token_id
        video_start_token_id = hf_config.video_start_token_id
        video_end_token_id = hf_config.video_end_token_id
        spatial_merge_size = hf_config.vision_config.spatial_merge_size
        llm_pos_ids_list: list = []

        if not (image_grid_thw is None and video_grid_thw is None):
            if isinstance(image_grid_thw, torch.Tensor):
                image_grid_thw = image_grid_thw.tolist()

            input_token_type: list[str] = []
            video_check_flg = False
            for token in input_tokens:
                if token == video_start_token_id:
                    video_check_flg = True
                elif token == video_end_token_id:
                    video_check_flg = False

                if (token == image_token_id) and (video_check_flg is False):
                    input_token_type.append("image")
                elif (token == image_token_id) and (video_check_flg is True):
                    input_token_type.append("video")
                else:
                    input_token_type.append("text")

            input_type_group: list[tuple[str, int, int]] = []
            for key, group_iter in itertools.groupby(
                    enumerate(input_token_type), lambda x: x[1]):
                group_list = list(group_iter)
                start_index = group_list[0][0]
                end_index = group_list[-1][0] + 1
                input_type_group.append((key, start_index, end_index))

            video_frame_num = 1
            mm_data_idx = 0
            for modality_type, start_idx, end_idx in input_type_group:
                st_idx = llm_pos_ids_list[-1].max() + 1 if len(
                    llm_pos_ids_list) > 0 else 0
                if modality_type == "image":
                    t, h, w = (
                        image_grid_thw[mm_data_idx][0],
                        image_grid_thw[mm_data_idx][1],
                        image_grid_thw[mm_data_idx][2],
                    )
                    llm_grid_t, llm_grid_h, llm_grid_w = \
                        t, h // spatial_merge_size, w // spatial_merge_size

                    t_index = torch.arange(llm_grid_t).view(-1, 1).expand(
                        -1, llm_grid_h * llm_grid_w).flatten()
                    h_index = torch.arange(llm_grid_h).view(1, -1, 1).expand(
                        llm_grid_t, -1, llm_grid_w).flatten()
                    w_index = torch.arange(llm_grid_w).view(1, 1, -1).expand(
                        llm_grid_t, llm_grid_h, -1).flatten()
                    llm_pos_ids_list.append(
                        torch.stack([t_index, h_index, w_index]) + st_idx)
                    mm_data_idx += 1

                elif modality_type == "video":
                    t, h, w = (
                        video_frame_num,
                        image_grid_thw[mm_data_idx][1],
                        image_grid_thw[mm_data_idx][2],
                    )
                    llm_grid_t, llm_grid_h, llm_grid_w = \
                        t, h // spatial_merge_size, w // spatial_merge_size

                    for t_idx in range(llm_grid_t):
                        t_index = torch.tensor(t_idx).view(-1, 1).expand(
                            -1, llm_grid_h * llm_grid_w).flatten()
                        h_index = torch.arange(llm_grid_h).view(
                            1, -1, 1).expand(1, -1, llm_grid_w).flatten()
                        w_index = torch.arange(llm_grid_w).view(
                            1, 1, -1).expand(1, llm_grid_h, -1).flatten()
                        llm_pos_ids_list.append(
                            torch.stack([t_index, h_index, w_index]) + st_idx)

                    mm_data_idx += 1
                    video_frame_num += 1

                else:
                    text_len = end_idx - start_idx
                    llm_pos_ids_list.append(
                        torch.arange(text_len).view(1, -1).expand(3, -1) +
                        st_idx)
                    video_frame_num = 1

        else:
            text_len = len(input_tokens)
            llm_pos_ids_list.append(
                torch.arange(text_len).view(1, -1).expand(3, -1))

        llm_positions = torch.cat(llm_pos_ids_list, dim=1).reshape(3, -1)
        llm_positions = llm_positions[:, context_len:seq_len]
        mrope_position_delta = (llm_positions.max() + 1 -
                                len(input_tokens)).item()
        return llm_positions, mrope_position_delta

    @classmethod
    def _vl_get_input_positions_tensor(
        cls,
        input_tokens: list[int],
        hf_config: PretrainedConfig,
        image_grid_thw: Union[list[list[int]], torch.Tensor],
        video_grid_thw: Union[list[list[int]], torch.Tensor],
        second_per_grid_ts: list[float],
        context_len: int = 0,
        seq_len: Optional[int] = None,
    ) -> tuple[torch.Tensor, int]:
        """Get mrope input positions and delta value."""

        image_token_id = hf_config.image_token_id
        video_token_id = hf_config.video_token_id
        vision_start_token_id = hf_config.vision_start_token_id
        spatial_merge_size = hf_config.vision_config.spatial_merge_size
        tokens_per_second = getattr(hf_config.vision_config,
                                    "tokens_per_second", 1.0)

        input_tokens_tensor = torch.tensor(input_tokens)
        vision_start_indices = torch.argwhere(
            input_tokens_tensor == vision_start_token_id).squeeze(1)
        vision_tokens = input_tokens_tensor[vision_start_indices + 1]
        image_nums = (vision_tokens == image_token_id).sum()
        video_nums = (vision_tokens == video_token_id).sum()
        llm_pos_ids_list: list = []

        st = 0
        remain_images, remain_videos = image_nums, video_nums

        image_index, video_index = 0, 0
        for _ in range(image_nums + video_nums):
            video_second_per_grid_t = 0.0
            if image_token_id in input_tokens and remain_images > 0:
                ed_image = input_tokens.index(image_token_id, st)
            else:
                ed_image = len(input_tokens) + 1
            if video_token_id in input_tokens and remain_videos > 0:
                ed_video = input_tokens.index(video_token_id, st)
            else:
                ed_video = len(input_tokens) + 1
            if ed_image < ed_video:
                t, h, w = (
                    image_grid_thw[image_index][0],
                    image_grid_thw[image_index][1],
                    image_grid_thw[image_index][2],
                )
                image_index += 1
                remain_images -= 1
                ed = ed_image
            else:
                t, h, w = (
                    video_grid_thw[video_index][0],
                    video_grid_thw[video_index][1],
                    video_grid_thw[video_index][2],
                )
                video_second_per_grid_t = 1.0
                if second_per_grid_ts:
                    video_second_per_grid_t = second_per_grid_ts[video_index]
                video_index += 1
                remain_videos -= 1
                ed = ed_video

            llm_grid_t, llm_grid_h, llm_grid_w = \
                t, h // spatial_merge_size, w // spatial_merge_size
            text_len = ed - st

            st_idx = llm_pos_ids_list[-1].max() + 1 if len(
                llm_pos_ids_list) > 0 else 0
            llm_pos_ids_list.append(
                torch.arange(text_len).view(1, -1).expand(3, -1) + st_idx)

            t_index = (torch.arange(llm_grid_t).view(-1, 1).expand(
                -1, llm_grid_h * llm_grid_w) * video_second_per_grid_t *
                       tokens_per_second).long().flatten()

            h_index = torch.arange(llm_grid_h).view(1, -1, 1).expand(
                llm_grid_t, -1, llm_grid_w).flatten()
            w_index = torch.arange(llm_grid_w).view(1, 1, -1).expand(
                llm_grid_t, llm_grid_h, -1).flatten()
            llm_pos_ids_list.append(
                torch.stack([t_index, h_index, w_index]) + text_len + st_idx)
            st = ed + llm_grid_t * llm_grid_h * llm_grid_w

        if st < len(input_tokens):
            st_idx = llm_pos_ids_list[-1].max() + 1 if len(
                llm_pos_ids_list) > 0 else 0
            text_len = len(input_tokens) - st
            llm_pos_ids_list.append(
                torch.arange(text_len).view(1, -1).expand(3, -1) + st_idx)

        llm_positions = torch.cat(llm_pos_ids_list, dim=1).reshape(3, -1)
        mrope_position_delta = (llm_positions.max() + 1 -
                                len(input_tokens)).item()
        llm_positions = llm_positions[:, context_len:seq_len]

        return llm_positions, mrope_position_delta

    @classmethod
    def _omni_get_input_positions_tensor(
        cls,
        input_tokens: list[int],
        hf_config: PretrainedConfig,
        image_grid_thw: Union[list[list[int]], torch.Tensor],
        video_grid_thw: Union[list[list[int]], torch.Tensor],
        second_per_grid_ts: Optional[list[float]] = None,
        context_len: int = 0,
        seq_len: Optional[int] = None,
        audio_feature_lengths: Optional[torch.Tensor] = None,
        use_audio_in_video: bool = False,
    ) -> tuple[torch.Tensor, int]:
        """Get mrope input positions and delta value (Qwen2.5-Omni version).

        Differences from MRotaryEmbedding:
            1. Add audio support (and related `audio_feature_lengths`).
            2. Add `use_audio_in_video` option to read audio from video inputs.
                In this case, audio and vision position ids will be split into
                chunks and interleaved.

        Example:

            (V_i are vision position ids, A_i are audio position ids)

            |V_1 ...    V_n|A_1 ...   A_n|V_n+1 ... V_2n|A_n+1 ... A_2n|...
            |vision chunk 1|audio chunk 1|vision chunk 2|audio chunk 2 |...
        """

        # TODO(fyabc): refactor and share more code with
        #  _vl_get_input_positions_tensor.

        thinker_config = hf_config.thinker_config
        audio_token_id = thinker_config.audio_token_index
        image_token_id = thinker_config.image_token_index
        video_token_id = thinker_config.video_token_index
        audio_start_token_id = thinker_config.audio_start_token_id
        audio_end_token_id = thinker_config.audio_end_token_id
        vision_start_token_id = thinker_config.vision_start_token_id
        vision_end_token_id = thinker_config.vision_end_token_id
        seconds_per_chunk = thinker_config.seconds_per_chunk
        spatial_merge_size = thinker_config.vision_config.spatial_merge_size
        tokens_per_second = getattr(thinker_config.vision_config,
                                    "tokens_per_second", 25)

        if isinstance(image_grid_thw, list):
            image_grid_thw = torch.tensor(image_grid_thw)
        if isinstance(video_grid_thw, list):
            video_grid_thw = torch.tensor(video_grid_thw)

        src_item = input_tokens
        audio_seqlens = audio_feature_lengths
        if not second_per_grid_ts:
            second_per_grid_ts = [1] * video_grid_thw.shape[0]
        audio_idx = 0
        video_idx = 0
        image_idx = 0
        new_src_item: list[int] = []
        llm_pos_ids_list: list[torch.Tensor] = []

        idx = 0
        while idx < len(src_item):
            new_src_item_len = len(new_src_item)
            start_idx = llm_pos_ids_list[-1].max() + 1 if len(
                llm_pos_ids_list) > 0 else 0
            if src_item[idx] not in [
                    audio_token_id, video_token_id, image_token_id
            ]:
                if use_audio_in_video and idx > 0:
                    if src_item[idx] == vision_end_token_id and \
                        src_item[idx - 1] == audio_end_token_id:
                        # processing the <|audio_eos|> before <|vision_eos|>
                        start_idx -= 1
                    elif src_item[idx] == audio_start_token_id and \
                        src_item[idx - 1] == vision_start_token_id:
                        # processing the <|audio_bos|> after <|vision_eos|>
                        start_idx -= 1
                new_src_item.append(src_item[idx])
                llm_pos_ids = torch.tensor([start_idx],
                                           dtype=torch.long).expand(3, -1)
                llm_pos_ids_list.append(llm_pos_ids)
            elif src_item[idx] == audio_token_id:
                assert audio_seqlens is not None
                audio_seqlen = audio_seqlens[audio_idx]
                place_num = (((audio_seqlen - 1) // 2 + 1 - 2) // 2 + 1)
                new_src_item.extend([audio_token_id] * place_num)
                llm_pos_ids = torch.arange(place_num).expand(3, -1) + start_idx
                llm_pos_ids_list.append(llm_pos_ids)
                audio_idx += 1
            elif src_item[idx] == image_token_id:
                grid_t = image_grid_thw[image_idx][0]
                grid_hs = image_grid_thw[:, 1]
                grid_ws = image_grid_thw[:, 2]
                t_index = (torch.arange(grid_t) * 1 * tokens_per_second).long()
                llm_pos_ids = cls._get_llm_pos_ids_for_vision(
                    start_idx, image_idx, spatial_merge_size, t_index, grid_hs,
                    grid_ws)
                llm_pos_ids_list.append(llm_pos_ids)
                vision_seqlen = image_grid_thw[image_idx].prod() // (
                    spatial_merge_size**2)
                new_src_item.extend([image_token_id] * vision_seqlen)
                image_idx += 1
            elif src_item[idx] == video_token_id and not use_audio_in_video:
                grid_t = video_grid_thw[video_idx][0]
                grid_hs = video_grid_thw[:, 1]
                grid_ws = video_grid_thw[:, 2]
                t_index = (torch.arange(grid_t) *
                           second_per_grid_ts[video_idx] *
                           tokens_per_second).long()
                llm_pos_ids = cls._get_llm_pos_ids_for_vision(
                    start_idx, video_idx, spatial_merge_size, t_index, grid_hs,
                    grid_ws)
                llm_pos_ids_list.append(llm_pos_ids)
                vision_seqlen = video_grid_thw[video_idx].prod() // (
                    spatial_merge_size**2)
                new_src_item.extend([video_token_id] * vision_seqlen)
                video_idx += 1
            else:
                # read audio from video
                assert audio_seqlens is not None
                audio_seqlen = audio_seqlens[audio_idx]
                vision_seqlen = video_grid_thw[video_idx].prod() // (
                    spatial_merge_size**2)
                grid_t = video_grid_thw[video_idx][0]
                grid_h = video_grid_thw[video_idx][1]
                grid_w = video_grid_thw[video_idx][2]
                grid_hs = video_grid_thw[:, 1]
                grid_ws = video_grid_thw[:, 2]
                t_ntoken_per_chunk = int(tokens_per_second * seconds_per_chunk)
                t_index = (torch.arange(grid_t) *
                           second_per_grid_ts[video_idx] *
                           tokens_per_second).long()
                t_index_split_chunk = cls._split_list_into_ranges(
                    t_index, t_ntoken_per_chunk)
                place_num = (((audio_seqlen - 1) // 2 + 1 - 2) // 2 + 1) + 2
                pure_audio_len = place_num - 2
                added_audio_len = 0
                audio_llm_pos_ids_list: list[torch.Tensor] = []
                for t_chunk in t_index_split_chunk:
                    vision_ntoken_per_chunk = len(
                        t_chunk) * grid_h * grid_w // (spatial_merge_size**2)
                    new_src_item.extend([video_token_id] *
                                        vision_ntoken_per_chunk)
                    vision_llm_pos_ids_list = cls._get_llm_pos_ids_for_vision(
                        start_idx, video_idx, spatial_merge_size, t_chunk,
                        grid_hs, grid_ws).split(1, dim=1)
                    llm_pos_ids_list.extend(vision_llm_pos_ids_list)
                    new_src_item.extend(
                        min(t_ntoken_per_chunk, pure_audio_len -
                            added_audio_len) * [audio_token_id])
                    audio_start_idx = start_idx if len(
                        audio_llm_pos_ids_list
                    ) == 0 else audio_llm_pos_ids_list[-1][0].item() + 1
                    if min(t_ntoken_per_chunk,
                           pure_audio_len - added_audio_len) > 0:
                        audio_llm_pos_ids_list = (torch.arange(
                            min(t_ntoken_per_chunk, pure_audio_len -
                                added_audio_len)).expand(3, -1) +
                                                  audio_start_idx).split(1,
                                                                         dim=1)
                    else:
                        audio_llm_pos_ids_list = []
                    added_audio_len += min(t_ntoken_per_chunk,
                                           pure_audio_len - added_audio_len)
                    llm_pos_ids_list.extend(audio_llm_pos_ids_list)
                if added_audio_len < pure_audio_len:
                    new_src_item.extend(
                        (pure_audio_len - added_audio_len) * [audio_token_id])
                    audio_llm_pos_ids_list = (
                        torch.arange(pure_audio_len - added_audio_len).expand(
                            3, -1) + llm_pos_ids_list[-1].max() + 1).split(
                                1, dim=1)
                    llm_pos_ids_list.extend(audio_llm_pos_ids_list)
                audio_idx += 1
                video_idx += 1
            # move to the next token
            idx += len(new_src_item) - new_src_item_len

        llm_positions = torch.cat(llm_pos_ids_list, dim=1)
        mrope_position_delta = torch.cat(llm_pos_ids_list,
                                         dim=1).max() + 1 - len(src_item)
        llm_positions = llm_positions[:, context_len:seq_len]

        return llm_positions, mrope_position_delta

    @staticmethod
    def _get_llm_pos_ids_for_vision(
        start_idx: int,
        vision_idx: int,
        spatial_merge_size: int,
        t_index: list[int],
        grid_hs: torch.Tensor,
        grid_ws: torch.Tensor,
    ) -> torch.Tensor:
        llm_pos_ids_list = []
        llm_grid_h = grid_hs[vision_idx] // spatial_merge_size
        llm_grid_w = grid_ws[vision_idx] // spatial_merge_size
        h_index = (torch.arange(llm_grid_h).view(1, -1, 1).expand(
            len(t_index), -1, llm_grid_w).flatten())
        w_index = (torch.arange(llm_grid_w).view(1, 1, -1).expand(
            len(t_index), llm_grid_h, -1).flatten())
        t_index_tensor = torch.Tensor(t_index).to(llm_grid_h.device).view(
            -1, 1).expand(-1, llm_grid_h * llm_grid_w).long().flatten()
        _llm_pos_ids = torch.stack([t_index_tensor, h_index, w_index])
        llm_pos_ids_list.append(_llm_pos_ids + start_idx)
        llm_pos_ids = torch.cat(llm_pos_ids_list, dim=1)
        return llm_pos_ids

    @staticmethod
    def _split_list_into_ranges(lst: torch.Tensor,
                                interval: int) -> list[list[int]]:
        ranges: list[list[int]] = [[]
                                   for _ in range((max(lst) // interval) + 1)]
        for num in lst:
            index = num // interval
            ranges[index].append(num)
        return ranges

    @staticmethod
    def get_next_input_positions(
        mrope_position_delta: int,
        context_len: int,
        seq_len: int,
    ) -> list[list[int]]:
        return [
            list(
                range(context_len + mrope_position_delta,
                      seq_len + mrope_position_delta)) for _ in range(3)
        ]

    @staticmethod
    def get_next_input_positions_tensor(out: np.ndarray, out_offset: int,
                                        mrope_position_delta: int,
                                        context_len: int, num_new_tokens: int):

        values = np.arange(mrope_position_delta + context_len,
                           mrope_position_delta + context_len + num_new_tokens,
                           dtype=out.dtype)
        out[:, out_offset:out_offset + num_new_tokens] = values

    @classmethod
    def omni_get_updates_use_audio_in_video(
        cls,
        thinker_config: PretrainedConfig,
        audio_len: int,
        video_grid_thw: Union[list[int], torch.Tensor],
        video_second_per_grid_t: float,
    ) -> list[int]:
        """Get video prompt updates when `use_audio_in_video` is True.

        In this case, audio and vision update ids will be split into
        chunks and interleaved (details in `_omni_get_input_positions_tensor`).

        <|video_bos|><|VIDEO|><|video_eos|> =>
        <|video_bos|><|audio_bos|>(... chunks ...)<|audio_eos|><|video_eos|>
        """

        audio_token_id = thinker_config.audio_token_index
        video_token_id = thinker_config.video_token_index
        audio_start_token_id = thinker_config.audio_start_token_id
        audio_end_token_id = thinker_config.audio_end_token_id
        seconds_per_chunk = thinker_config.seconds_per_chunk
        spatial_merge_size = thinker_config.vision_config.spatial_merge_size
        tokens_per_second = getattr(thinker_config.vision_config,
                                    "tokens_per_second", 25)

        grid_t = video_grid_thw[0]
        grid_h = video_grid_thw[1]
        grid_w = video_grid_thw[2]
        t_ntoken_per_chunk = int(tokens_per_second * seconds_per_chunk)
        t_index = (torch.arange(grid_t) * video_second_per_grid_t *
                   tokens_per_second).long()
        t_index_split_chunk = cls._split_list_into_ranges(
            t_index, t_ntoken_per_chunk)

        updates = [audio_start_token_id]
        added_audio_len = 0
        for t_chunk in t_index_split_chunk:
            vision_ntoken_per_chunk = len(t_chunk) * grid_h * grid_w // (
                spatial_merge_size**2)
            updates.extend([video_token_id] * vision_ntoken_per_chunk)

            audio_chunk_size = min(t_ntoken_per_chunk,
                                   audio_len - added_audio_len)
            updates.extend(audio_chunk_size * [audio_token_id])
            added_audio_len += audio_chunk_size
        if added_audio_len < audio_len:
            updates.extend((audio_len - added_audio_len) * [audio_token_id])
        updates.extend([audio_end_token_id])

        return updates


@CustomOp.register("dual_chunk_rotary_embedding")
class DualChunkRotaryEmbedding(CustomOp):
    """Rotary positional embedding for Dual Chunk Attention."""

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: float,
        is_neox_style: bool,
        dtype: torch.dtype,
        chunk_size: int,
        local_size: int,
    ) -> None:
        super().__init__()
        self.head_size = head_size
        self.rotary_dim = rotary_dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        self.is_neox_style = is_neox_style
        self.chunk_size = chunk_size
        self.local_size = local_size
        self.dtype = dtype
        self.device = torch.device(f"cuda:{torch.cuda.current_device()}")
        (q_cache, qc_cache, k_cache, qc_no_clamp_cache,
         q_inter_cache) = self._compute_cos_sin_cache()

        self.register_buffer("cos_sin_q_cache", q_cache, persistent=False)
        self.register_buffer("cos_sin_qc_cache", qc_cache, persistent=False)
        self.register_buffer("cos_sin_k_cache", k_cache, persistent=False)
        self.register_buffer("cos_sin_qc_no_clamp_cache",
                             qc_no_clamp_cache,
                             persistent=False)
        self.register_buffer("cos_sin_q_inter_cache",
                             q_inter_cache,
                             persistent=False)

    def _compute_inv_freq(self, base: float) -> torch.Tensor:
        """Compute the inverse frequency."""
        # NOTE(woosuk): The HF implementation uses `torch.arange(...).float()`.
        # However, we use `torch.arange(..., dtype=torch.float)` instead to
        # avoid numerical issues with large base values (e.g., 10000000).
        # This may cause a slight numerical difference between the HF
        # implementation and ours.
        # NOTE(woosuk): To exactly match the HF implementation, we need to
        # use CPU to compute the cache and then move it to GPU. However, we
        # create the cache on GPU for faster initialization. This may cause
        # a slight numerical difference between the HF implementation and ours.
        inv_freq = 1.0 / (base**(torch.arange(
            0, self.rotary_dim, 2, dtype=torch.float) / self.rotary_dim))
        return inv_freq

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        """Compute the cos and sin cache."""
        inv_freq = self._compute_inv_freq(self.base)
        chunk_len = self.chunk_size - self.local_size
        q_t = torch.arange(chunk_len, dtype=torch.float)
        qc_t = (torch.arange(chunk_len, dtype=torch.float) +
                chunk_len).clamp(max=self.chunk_size)
        k_t = torch.arange(self.max_position_embeddings,
                           dtype=torch.float) % chunk_len

        # count from chunk_len, no clamp(self.chunk_size) restriction
        qc_no_clamp_t = torch.arange(chunk_len, dtype=torch.float) + chunk_len
        # count from self.chunk_size for q_inter's rope
        q_inter_t = torch.arange(chunk_len,
                                 dtype=torch.float) + self.chunk_size

        q_freqs = torch.outer(q_t, inv_freq)
        qc_freqs = torch.outer(qc_t, inv_freq)
        k_freqs = torch.outer(k_t, inv_freq)
        qc_no_clamp_freqs = torch.outer(qc_no_clamp_t, inv_freq)
        q_inter_freqs = torch.outer(q_inter_t, inv_freq)

        q_cos = q_freqs.cos()
        q_sin = q_freqs.sin()
        qc_cos = qc_freqs.cos()
        qc_sin = qc_freqs.sin()
        k_cos = k_freqs.cos()
        k_sin = k_freqs.sin()

        qc_no_clamp_cos = qc_no_clamp_freqs.cos()
        qc_no_clamp_sin = qc_no_clamp_freqs.sin()
        q_inter_cos = q_inter_freqs.cos()
        q_inter_sin = q_inter_freqs.sin()

        q_cache = torch.cat((q_cos, q_sin), dim=-1).to(dtype=self.dtype,
                                                       device=self.device)
        qc_cache = torch.cat((qc_cos, qc_sin), dim=-1).to(dtype=self.dtype,
                                                          device=self.device)
        k_cache = torch.cat((k_cos, k_sin), dim=-1).to(dtype=self.dtype,
                                                       device=self.device)
        qc_no_clamp_cache = torch.cat((qc_no_clamp_cos, qc_no_clamp_sin),
                                      dim=-1).to(dtype=self.dtype,
                                                 device=self.device)
        q_inter_cache = torch.cat((q_inter_cos, q_inter_sin),
                                  dim=-1).to(dtype=self.dtype,
                                             device=self.device)
        return q_cache, qc_cache, k_cache, qc_no_clamp_cache, q_inter_cache

    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: torch.Tensor,
        offsets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        query = query.view(*query.shape[:-1], -1, self.head_size)
        key = key.view(*key.shape[:-1], -1, self.head_size)
        query_rot = query[..., :self.rotary_dim]
        key_rot = key[..., :self.rotary_dim]
        if self.rotary_dim < self.head_size:
            query_pass = query[..., self.rotary_dim:]
            key_pass = key[..., self.rotary_dim:]
        else:
            query_pass = None
            key_pass = None

        positions_with_offsets = (torch.add(positions, offsets)
                                  if offsets is not None else positions)
        key = self._apply_rotary_embedding(
            self.cos_sin_k_cache[positions_with_offsets], key_rot, key_pass)
        chunk_len = self.chunk_size - self.local_size
        query = self._apply_rotary_embedding(
            self.cos_sin_q_cache[positions_with_offsets % chunk_len],
            query_rot, query_pass)
        query_succ = self._apply_rotary_embedding(
            self.cos_sin_qc_cache[positions_with_offsets % chunk_len],
            query_rot, query_pass)
        query_inter = self._apply_rotary_embedding(
            self.cos_sin_qc_cache[chunk_len - 1].repeat(positions.shape[0], 1),
            query_rot, query_pass)
        query_succ_critical = self._apply_rotary_embedding(
            self.cos_sin_qc_no_clamp_cache[positions_with_offsets % chunk_len],
            query_rot, query_pass)
        query_inter_critical = self._apply_rotary_embedding(
            self.cos_sin_q_inter_cache[positions_with_offsets % chunk_len],
            query_rot, query_pass)

        # merge query into one tensor to simplify the interfaces
        query = torch.cat((
            query,
            query_succ,
            query_inter,
            query_succ_critical,
            query_inter_critical,
        ),
                          dim=-1)
        return query, key

    def _apply_rotary_embedding(self, cos_sin, hidden_rot, hidden_pass):
        cos, sin = cos_sin.chunk(2, dim=-1)
        if self.is_neox_style:
            # NOTE(woosuk): Here we assume that the positions tensor has the
            # shape [batch_size, seq_len].
            cos = cos.repeat(1, 1, 2).unsqueeze(-2)
            sin = sin.repeat(1, 1, 2).unsqueeze(-2)
        else:
            cos = cos.repeat_interleave(2, dim=-1).unsqueeze(-2)
            sin = sin.repeat_interleave(2, dim=-1).unsqueeze(-2)
        rotate_fn = _rotate_neox if self.is_neox_style else _rotate_gptj
        hidden_rot = hidden_rot * cos + rotate_fn(hidden_rot) * sin

        if self.rotary_dim < self.head_size:
            hidden = torch.cat((hidden_rot, hidden_pass), dim=-1)
        else:
            hidden = hidden_rot
        return hidden.flatten(-2).squeeze(0)

    def extra_repr(self) -> str:
        s = f"head_size={self.head_size}, rotary_dim={self.rotary_dim}"
        s += f", max_position_embeddings={self.max_position_embeddings}"
        s += f", base={self.base}, is_neox_style={self.is_neox_style}"
        s += f", chunk_size={self.chunk_size}, local_size={self.local_size}"
        return s


_ROPE_DICT: dict[tuple, RotaryEmbedding] = {}


def get_rope(
    head_size: int,
    rotary_dim: int,
    max_position: int,
    base: float,
    is_neox_style: bool = True,
    rope_scaling: Optional[dict[str, Any]] = None,
    dtype: Optional[torch.dtype] = None,
    partial_rotary_factor: float = 1.0,
    dual_chunk_attention_config: Optional[dict[str, Any]] = None,
) -> RotaryEmbedding:
    if dtype is None:
        dtype = torch.get_default_dtype()
    if rope_scaling is not None:
        # Transforms every value that is a list into a tuple for caching calls
        rope_scaling_tuple = {
            k: tuple(v) if isinstance(v, list) else v
            for k, v in rope_scaling.items()
        }
        rope_scaling_args = tuple(rope_scaling_tuple.items())
    else:
        rope_scaling_args = None

    if dual_chunk_attention_config is not None:
        dual_chunk_attention_tuple = {
            k: tuple(v) if isinstance(v, list) else v
            for k, v in dual_chunk_attention_config.items()
            if k != "sparse_attention_config"
        }
        dual_chunk_attention_args = tuple(dual_chunk_attention_tuple.items())
    else:
        dual_chunk_attention_args = None

    if partial_rotary_factor < 1.0:
        rotary_dim = int(rotary_dim * partial_rotary_factor)
    key = (head_size, rotary_dim, max_position, base, is_neox_style,
           rope_scaling_args, dual_chunk_attention_args, dtype)
    if key in _ROPE_DICT:
        return _ROPE_DICT[key]

    if dual_chunk_attention_config is not None:
        extra_kwargs = {
            k: v
            for k, v in dual_chunk_attention_config.items()
            if k in ("chunk_size", "local_size")
        }
        rotary_emb = DualChunkRotaryEmbedding(head_size, rotary_dim,
                                              max_position, base,
                                              is_neox_style, dtype,
                                              **extra_kwargs)
    elif not rope_scaling:
        rotary_emb = RotaryEmbedding(head_size, rotary_dim, max_position, base,
                                     is_neox_style, dtype)
    else:
        scaling_type = rope_scaling["rope_type"]

        if scaling_type == "llama3":
            scaling_factor = rope_scaling["factor"]
            low_freq_factor = rope_scaling["low_freq_factor"]
            high_freq_factor = rope_scaling["high_freq_factor"]
            original_max_position = rope_scaling[
                "original_max_position_embeddings"]
            rotary_emb = Llama3RotaryEmbedding(head_size, rotary_dim,
                                               max_position, base,
                                               is_neox_style, dtype,
                                               scaling_factor, low_freq_factor,
                                               high_freq_factor,
                                               original_max_position)
        elif scaling_type == "mllama4":
            rotary_emb = Llama4VisionRotaryEmbedding(head_size, rotary_dim,
                                                     max_position, base,
                                                     is_neox_style, dtype)
        elif scaling_type == "default":
            if "mrope_section" in rope_scaling:
                rotary_emb = MRotaryEmbedding(
                    head_size,
                    rotary_dim,
                    max_position,
                    base,
                    is_neox_style,
                    dtype,
                    mrope_section=rope_scaling["mrope_section"],
                )
            else:
                rotary_emb = RotaryEmbedding(
                    head_size,
                    rotary_dim,
                    max_position,
                    base,
                    is_neox_style,
                    dtype,
                )
        elif scaling_type == "linear":
            scaling_factor = rope_scaling["factor"]
            rotary_emb = LinearScalingRotaryEmbedding(head_size, rotary_dim,
                                                      max_position, base,
                                                      is_neox_style,
                                                      scaling_factor, dtype)
        elif scaling_type == "ntk":
            scaling_factor = rope_scaling["factor"]
            mixed_b = rope_scaling.get('mixed_b', None)
            rotary_emb = NTKScalingRotaryEmbedding(head_size, rotary_dim,
                                                   max_position, base,
                                                   is_neox_style,
                                                   scaling_factor, dtype,
                                                   mixed_b)
        elif scaling_type == "dynamic":
            if "alpha" in rope_scaling:
                scaling_alpha = rope_scaling["alpha"]
                rotary_emb = DynamicNTKAlphaRotaryEmbedding(
                    head_size, rotary_dim, max_position, base, is_neox_style,
                    scaling_alpha, dtype)
            elif "factor" in rope_scaling:
                scaling_factor = rope_scaling["factor"]
                rotary_emb = DynamicNTKScalingRotaryEmbedding(
                    head_size, rotary_dim, max_position, base, is_neox_style,
                    scaling_factor, dtype)
            else:
                raise ValueError("Dynamic rope scaling must contain either "
                                 "'alpha' or 'factor' field")
        elif scaling_type == "yarn":
            scaling_factor = rope_scaling["factor"]
            original_max_position = rope_scaling[
                "original_max_position_embeddings"]
            extra_kwargs = {
                k: v
                for k, v in rope_scaling.items()
                if k in ("extrapolation_factor", "attn_factor", "beta_fast",
                         "beta_slow")
            }
            rotary_emb = YaRNScalingRotaryEmbedding(head_size, rotary_dim,
                                                    original_max_position,
                                                    base, is_neox_style,
                                                    scaling_factor, dtype,
                                                    **extra_kwargs)
        elif scaling_type == "deepseek_yarn":
            scaling_factor = rope_scaling["factor"]
            original_max_position = rope_scaling[
                "original_max_position_embeddings"]
            # assert max_position == original_max_position * scaling_factor
            extra_kwargs = {
                k: v
                for k, v in rope_scaling.items()
                if k in ("extrapolation_factor", "attn_factor", "beta_fast",
                         "beta_slow", "mscale", "mscale_all_dim")
            }
            rotary_emb = DeepseekScalingRotaryEmbedding(
                head_size, rotary_dim, original_max_position, base,
                is_neox_style, scaling_factor, dtype, **extra_kwargs)
        elif scaling_type == "longrope":
            short_factor = rope_scaling["short_factor"]
            long_factor = rope_scaling["long_factor"]
            original_max_position = rope_scaling[
                "original_max_position_embeddings"]
            extra_kwargs = {
                k: v
                for k, v in rope_scaling.items()
                if k in ("short_mscale", "long_mscale")
            }
            rotary_emb = Phi3LongRoPEScaledRotaryEmbedding(
                head_size, rotary_dim, max_position, original_max_position,
                base, is_neox_style, dtype, short_factor, long_factor,
                **extra_kwargs)
        else:
            raise ValueError(f"Unknown RoPE scaling type {scaling_type}")
    _ROPE_DICT[key] = rotary_emb
    return rotary_emb
