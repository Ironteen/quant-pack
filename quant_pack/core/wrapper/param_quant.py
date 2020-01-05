# -*- coding: utf-8 -*-

import re
import copy
from types import MethodType
from contextlib import contextmanager
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

from quant_pack.core.quant.config import QuantConfig
from quant_pack.core.train.qat_policies import VALID_QUANT_MODE
from quant_pack.core.wrapper.hook.calibration_builder import ActivationCalibrationBuilder
from ._registries import FUSED_FORWARD_FUNCTIONS, \
    QUANT_FORWARD_FUNCTIONS


def _get_submodule(module, sub_name):
    for n in sub_name.split("."):
        module = getattr(module, n)
    return module


def _register_parameters(module, *named_params):
    for name, param in named_params:
        module.register_parameter(name, param)


class ParametrizedQuantWrapper(nn.Module):

    _quantable_types = tuple(QUANT_FORWARD_FUNCTIONS.keys())

    def __init__(self, module, quant_conf, bn_folding_mapping, do_fold_bn, fp_layers=None):
        """Model wrapper for parameterized-quantized training/evaluation.

        Args:
            module (nn.Module): the underlying task-agnostic model;
            quant_conf (dict): whose content will pass to `QuantConfig`
                - mode:
                - bit_width:
                - align_zero:
            bn_folding_mapping (list[tuple]): mapping from `BN layer name` ->
                `Conv/FC layer name`;
            do_fold_bn (bool): whether actually do BN folding training/inference
            fp_layers (list[str], optional)
        """
        super(ParametrizedQuantWrapper, self).__init__()

        assert not isinstance(module, DistributedDataParallel), \
            f"`module` should not wrapped with DDP, since the initialization of" \
            f"{self.__class__.__name__} will register additional parameters to" \
            f"`module`. Pass in the raw module then call `to_ddp()` instead."

        self.module = module
        self.quant_conf = quant_conf
        self.quant_mode = None  # setup by qat_hooks later
        self._in_qat = False  # add this flag so `HijackModuleOutput` knows whether itself be enabled
        self._module_forward = module.__class__.forward
        self._quant_submodules = set()
        self._fused_submodules = set()

        self._do_bn_folding(bn_folding_mapping, do_fold_bn)
        self._register_quant_params(fp_layers)

    def _do_bn_folding(self, bn_folding_mapping, do_fold_bn):
        # decorate Conv2d such that its instances can get proper running statistics based on `input_qconf`
        @property
        def running_mean(module):
            if hasattr(module, "input_qconf") and module.input_qconf.enabled:
                return module._running_mean_q
            else:
                return module._running_mean_fp

        @property
        def running_var(module):
            if hasattr(module, "input_qconf") and module.input_qconf.enabled:
                return module._running_var_q
            else:
                return module._running_var_fp

        nn.Conv2d.running_mean = running_mean
        nn.Conv2d.running_var = running_var

        for (bn_name, conv_name) in bn_folding_mapping:
            bn_layer = _get_submodule(self.module, bn_name)
            conv_layer = _get_submodule(self.module, conv_name)
            assert isinstance(bn_layer, nn.BatchNorm2d) and isinstance(conv_layer, nn.Conv2d)
            assert bn_layer._version >= 2, "deprecated BN implementation, please update to PyTorch>=1.1"

            conv_layer.register_parameter("alpha", bn_layer.weight)
            conv_layer.register_parameter("beta", bn_layer.bias)
            conv_layer.register_buffer("_running_mean_fp", bn_layer.running_mean.clone())
            conv_layer.register_buffer("_running_var_fp", bn_layer.running_var.clone())
            conv_layer.register_buffer("_running_mean_q", bn_layer.running_mean.clone())
            conv_layer.register_buffer("_running_var_q", bn_layer.running_var.clone())
            conv_layer.affine = bn_layer.affine
            conv_layer.bn_momentum = bn_layer.momentum
            conv_layer.bn_eps = bn_layer.eps
            bn_layer._parameters.clear()
            bn_layer._buffers.clear()

            # place holders, filled by pre_iter_hooks from m.*qconf later
            conv_layer.weight_transform = conv_layer.input_transform = None
            conv_layer.fold_bn = do_fold_bn
            conv_layer.forward = MethodType(FUSED_FORWARD_FUNCTIONS[conv_layer.__class__], conv_layer)
            bn_layer.forward = MethodType(FUSED_FORWARD_FUNCTIONS[bn_layer.__class__], bn_layer)

            self._fused_submodules.add(conv_layer)
            self._fused_submodules.add(bn_layer)

    def _register_quant_params(self, fp_layers):
        if fp_layers is not None:
            fp_layers = [re.compile(r) for r in fp_layers]
        for n, m in self.module.named_modules():
            if isinstance(m, self._quantable_types):
                if fp_layers is not None and any(reg.match(n) for reg in fp_layers):
                    pass
                else:
                    _register_parameters(
                        m,
                        ("w_lb", nn.Parameter(m.weight.detach().min())),
                        ("w_ub", nn.Parameter(m.weight.detach().max())),
                        ("a_lb", nn.Parameter(torch.tensor(0.))),
                        ("a_ub", nn.Parameter(torch.tensor(1.))),
                    )
                    m.weight_qconf = QuantConfig(lb=m.w_lb, ub=m.w_ub, **self.quant_conf)
                    m.input_qconf = QuantConfig(lb=m.a_lb, ub=m.a_ub, **self.quant_conf)
                    self._quant_submodules.add(m)

                    if m not in self._fused_submodules:
                        m.weight_transform = m.input_transform = None
                        m.forward = MethodType(QUANT_FORWARD_FUNCTIONS[m.__class__], m)

    def to_ddp(self, find_unused_parameters=True):
        assert dist.is_available() and dist.is_initialized()
        if isinstance(self.module, DistributedDataParallel):
            raise RuntimeError("`module` is already DDP")
        self.module = DistributedDataParallel(self.module, device_ids=[torch.cuda.current_device()],
                                              find_unused_parameters=find_unused_parameters)

    def to_torch_quant(self):
        raise NotImplementedError()

    def get_optimizers(self, *optim_grops):
        ret = {}
        named_params = dict(self.module.named_parameters())
        for optim_group in optim_grops:
            optim_name = optim_group["name"]
            optim_type = optim_group["optim_type"]
            optim_matches = [re.compile(match) for match in optim_group["matches"]]
            optim_params = copy.copy(optim_group["args"])
            optim_params["params"] = []
            matched_names = []
            for name, param in named_params.items():
                if any(pattern.match(name) for pattern in optim_matches):
                    matched_names.append(name)
                    optim_params["params"].append(param)
            for name in matched_names:
                named_params.pop(name)
            optim = torch.optim.__dict__[optim_type]([optim_params], **optim_group["args"])
            ret[optim_name] = optim
        if len(ret) == 1:
            ret = next(iter(ret.values()))
        return ret

    def quant_w(self, enabled=True):
        for m in self._quant_submodules:
            m.weight_qconf.quant(enabled)
            m.weight_transform = m.weight_qconf.transform

    def fp_w(self):
        self.quant_w(enabled=False)

    def quant_a(self, enabled=True):
        for m in self._quant_submodules:
            m.input_qconf.quant(enabled)
            m.input_transform = m.input_qconf.transform

    def fp_a(self):
        self.quant_a(enabled=False)

    @contextmanager
    def _inject_runtime_hooks(self, runtime_hooks):
        need_recover = False
        handles = []
        if runtime_hooks:
            if isinstance(self.module, DistributedDataParallel):
                # 前方高能，套娃警告
                def _ddp_forward(_module, *_args, **_kwargs):
                    _handles = []
                    for _n, _m in _module.named_modules():
                        for _hook in runtime_hooks:
                            if _hook.match(_n, _m):
                                for _method, _hook in _hook.get_hooks():
                                    _handle = getattr(_m, _method)(_hook)
                                    _handles.append(_handle)
                    _outputs = self._module_forward(_module, *_args, **_kwargs)
                    for _handle in _handles:
                        _handle.remove()
                    return _outputs

                self.module.module.forward = MethodType(_ddp_forward, self.module.module)
                need_recover = True
            else:
                for n, m in self.module.named_modules():
                    for hook in runtime_hooks:
                        if hook.match(n, m):
                            for method, hook in hook.get_hooks():
                                handle = getattr(m, method)(hook)
                                handles.append(handle)
        try:
            yield
        finally:
            if need_recover:
                self.module.module.forward = MethodType(self._module_forward, self.module.module)
            for handle in handles:
                handle.remove()

    def forward(self, *inputs, runtime_hooks=None, **kwargs):
        with self._inject_runtime_hooks(runtime_hooks):
            outputs = self.module(*inputs, **kwargs)
        return outputs

    @staticmethod
    def batch_processor(model, data_batch, train_mode, device, quant_mode=None):
        if quant_mode is None:
            quant_mode = model.quant_mode
            if any(mode != "fp" for mode in quant_mode):
                model._in_qat = True
            else:
                model._in_qat = False
        runtime_hooks = getattr(model, "runtime_hooks", {})
        img, label = data_batch
        outputs = {"label": label.to(device, non_blocking=True)}
        for i, mode in enumerate(quant_mode):
            assert mode in VALID_QUANT_MODE, f"invalid model running mode: {mode}"
            if mode == "fp":
                model.fp_w()
                model.fp_a()
            elif mode == "quant":
                model.quant_w()
                model.quant_a()
            elif mode == "qw_fa":
                model.quant_w()
                model.fp_a()
            elif mode == "fw_qa":
                model.fp_w()
                model.quant_a()

            activated_hooks = []
            for _, hook in runtime_hooks.items():
                if hasattr(hook, "set_output_reg"):
                    hook.set_output_reg(outputs)
                if hook.inject_at(mode):
                    activated_hooks.append(hook)

            outputs[mode] = model(img.to(device, non_blocking=True), runtime_hooks=activated_hooks)

        return outputs

    @torch.no_grad()
    def do_calibration(self, runner, calibration_step, calibration_percentile, device):
        runner.logger.info(f"start calibration at epoch {runner.epoch}, iter {runner.iter}")
        for m in self._quant_submodules:
            # TODO: handle BN-folding?
            m.w_lb.copy_(m.weight.min())
            m.w_ub.copy_(m.weight.max())
        self.quant_w()
        self.fp_a()
        act_calib_hook = ActivationCalibrationBuilder(calibration_percentile)
        for i, data_batch in enumerate(runner.data_loader):
            if i >= calibration_step:
                break
            img, _ = data_batch
            _ = self(img.to(device, non_blocking=True), runtime_hooks=[act_calib_hook])
        for m in self._fused_submodules:
            if not isinstance(m, nn.BatchNorm2d):
                m._running_mean_q = m._running_mean_fp.clone()
                m._running_var_q = m._running_var_fp.clone()
        runner.logger.info(f"calibration done")
