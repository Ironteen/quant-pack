# -*- coding: utf-8 -*-

import os
import logging
import json
from argparse import ArgumentParser
from datetime import datetime
from copy import deepcopy
from pprint import pformat

import yaml
import torch
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn import SyncBatchNorm
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from easydict import EasyDict
from tqdm import tqdm

from quant_prob import modeling
from quant_prob.utils import *

BEST_ACCURACY = 0.
EXP_DATETIME = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
CONF = None
DEVICE = None
TB_LOGGER = None
LOGGER_NAME = "global"
RANK = 0
WORLD_SIZE = 1
SEED = 19260817


def main():
    global BEST_ACCURACY, CONF, DEVICE, TB_LOGGER, RANK, WORLD_SIZE

    parser = ArgumentParser(f"Probabilistic quantization neural networks.")
    parser.add_argument("--conf-path", "-c", required=True, help="path of configuration file")
    parser.add_argument("--port", "-p", type=int, required=True, help="port of distributed backend")
    parser.add_argument("--evaluate_only", "-e", action="store_true", help="evaluate trained model")
    parser.add_argument("--extra", "-x", type=json.loads, help="extra configurations in json format")
    parser.add_argument("--comment", "-m", default="", help="comment for each experiment")
    parser.add_argument("--debug", action="store_true", help="logging debug info")
    args = parser.parse_args()

    with open(args.conf_path, "r", encoding="utf-8") as f:
        CONF = yaml.load(f, Loader=yaml.SafeLoader)
        cli_conf = {k: v for k, v in vars(args).items() if k != "extra" and not k.startswith("__")}
        update_config(CONF, cli_conf)
        if args.extra is not None:
            update_config(CONF, args.extra)
        CONF = EasyDict(CONF)

    RANK, WORLD_SIZE = dist_init(CONF.port)
    CONF.dist = WORLD_SIZE > 1
    logger = init_log(LOGGER_NAME, CONF.debug, f"{CONF.log.file}_{EXP_DATETIME}.log")
    DEVICE = torch.device(f"cuda:{torch.cuda.current_device()}") if torch.cuda.is_available() else torch.device("cpu")

    torch.manual_seed(SEED)
    torch.backends.cudnn.benchmark = True

    logger.debug(f"configurations:\n{pformat(CONF)}")
    logger.debug(f"device: {DEVICE}")

    logger.debug(f"building dataset {CONF.data.dataset.type}...")
    train_set, val_set = get_dataset(CONF.data.dataset.type, **CONF.data.dataset.args)
    logger.debug(f"building training loader...")
    train_loader = DataLoader(train_set,
                              sampler=IterationSampler(train_set, rank=RANK, world_size=WORLD_SIZE,
                                                       **CONF.data.train_sampler_conf),
                              **CONF.data.train_loader_conf)
    logger.debug(f"building validation loader...")
    val_loader = DataLoader(val_set,
                            sampler=DistributedSampler(val_set) if CONF.dist else None,
                            **CONF.data.val_loader_conf)

    logger.debug(f"building model `{CONF.arch.type}`...")
    model = modeling.__dict__[CONF.arch.type](**CONF.arch.args).to(DEVICE, non_blocking=True)
    if CONF.dist and CONF.arch.sync_bn:
        model = SyncBatchNorm.convert_sync_batchnorm(model)
    logger.debug(f"build model {model.__class__.__name__} done:\n{model}")

    param_groups = model.get_param_group(*CONF.param_group.groups, **CONF.param_group.args)
    opt = HybridOpt(param_groups, CONF.param_group.conf, **CONF.opt.args)
    scheduler = IterationScheduler(opt.optimizers[0], dataset_size=len(train_set), world_size=WORLD_SIZE,
                                   **CONF.schedule.args)

    if CONF.dist:
        logger.debug(f"building DDP model...")
        model, model_without_ddp = get_ddp_model(model)
    else:
        model_without_ddp = model

    if CONF.log.tb_dir is not None and RANK == 0 and not CONF.evaluate_only:
        tb_dir = f"{EXP_DATETIME}_{CONF.comment}" if CONF.comment is not "" else f"{EXP_DATETIME}"
        tb_dir = os.path.join(CONF.log.tb_dir, tb_dir)
        logger.debug(f"creating TensorBoard at: {tb_dir}...")
        os.makedirs(tb_dir, exist_ok=True)
        TB_LOGGER = SummaryWriter(tb_dir)

    if CONF.resume.path is not None:
        if CONF.resume.path == "latest":
            resume_path = get_latest_file(CONF.ckpt.dir)
        elif CONF.resume.path == "final":
            resume_path = os.path.join(CONF.ckpt.dir, "ckpt_final.pth")
        else:
            resume_path = CONF.resume.path
        logger.debug(f"loading checkpoint at: {resume_path}...")
        with open(resume_path, "rb") as f:
            ckpt = torch.load(f, DEVICE)
            model_dict = ckpt["model"] if "model" in ckpt.keys() else ckpt
            try:
                model_without_ddp.load_state_dict(model_dict, strict=False)
            except RuntimeError as e:
                logger.warning(e)
            if CONF.resume.load_opt:
                logger.debug(f"recovering optimizer...")
                opt.load_state_dict(ckpt["opt"])
            if CONF.resume.load_scheduler:
                BEST_ACCURACY = ckpt["accuracy"]
                scheduler.load_state_dict(ckpt["scheduler"])
                train_loader.sampler.set_last_iter(scheduler.last_iter)
                logger.debug(f"recovered opt at iteration: {scheduler.last_iter}")

    if CONF.teacher_arch is not None:
        logger.debug(f"building FP teacher model {CONF.teacher_arch.type}...")
        teacher = modeling.__dict__[CONF.teacher_arch.type](**CONF.teacher_arch.args).to(DEVICE, non_blocking=True)
        with open(CONF.teacher_arch.ckpt, "rb") as f:
            ckpt = torch.load(f, DEVICE)
            teacher.load_state_dict(ckpt)
        for p in teacher.parameters():
            p.requires_grad = False
    else:
        teacher = None

    logger.debug(f"building criterion {CONF.loss.type}...")
    criterion = get_loss(CONF.loss.type, **CONF.loss.args)

    if CONF.debug:
        num_params = 0
        opt_conf = []
        for p in opt.get_param_groups():
            num_params += len(p["params"])
            opt_conf.append({k: v for k, v in p.items() if k != "params"})
        logger.debug(f"number of parameters: {num_params}")
        logger.debug(f"optimizer conf:\n{pformat(opt_conf)}")

    if CONF.diagnose.enabled:
        logger.debug(f"building diagnoser `{CONF.diagnose.diagnoser.type}` with conf: "
                     f"\n{pformat(CONF.diagnose.diagnoser.args)}")
        model = get_diagnoser(CONF.diagnose.diagnoser.type, model, logger=TB_LOGGER, **CONF.diagnose.diagnoser.args)
        get_tasks(model, CONF.diagnose.tasks)  # TODO: should we preserve these tasks?

    if CONF.evaluate_only:
        if CONF.eval.calibrate:
            logger.info(f"calibrating quantization ranges at iteration {scheduler.last_iter}...")
            model_without_ddp.update_ddp_quant_param(
                model,
                val_loader,
                CONF.quant.calib.steps,
                CONF.quant.calib.gamma,
                CONF.quant.calib.update_bn,
            )
        logger.info(f"[Step {scheduler.last_iter}]: evaluating...")
        evaluate(model, val_loader, enable_quant=CONF.eval.quant, use_ema_stat=CONF.eval.use_ema_stat, verbose=True)
        return

    train(model, criterion, train_loader, val_loader, opt, scheduler, teacher)


def train(model, criterion, train_loader, val_loader, opt, scheduler, teacher_model=None):
    global BEST_ACCURACY
    logger = logging.getLogger(LOGGER_NAME)
    checkpointer = Checkpointer(CONF.ckpt.dir)
    model_without_ddp = model.module if CONF.dist else model
    metric_logger = MetricLogger(TB_LOGGER, last_iter=scheduler.last_iter)
    metric_logger.add_meter("LR", SmoothedValue(fmt="{value:.4f}"))
    metric_logger.add_meter("loss", SmoothedValue(fmt="{value:.4f}"))
    model.train()

    for img, label in metric_logger.log_every(train_loader, CONF.log.freq,
                                              log_prefix="train", progress_bar=CONF.progress_bar):
        scheduler.step()
        step = scheduler.last_iter

        if CONF.quant.calib.required_on_training and scheduler.do_calibration:
            logger.info(f"resetting quantization ranges at iteration {scheduler.last_iter}...")
            model_without_ddp.update_ddp_quant_param(
                model,
                train_loader,
                CONF.quant.calib.steps,
                CONF.quant.calib.gamma,
                CONF.quant.calib.update_bn,
            )
            if CONF.distil.zero_momentum:
                logger.debug(f"clear optimizer momentum after calibration")
                opt.zero_momentum()
            checkpointer.save(
                model=model_without_ddp.state_dict(),
                opt=opt.state_dict(),
                scheduler=scheduler.state_dict(),
                accuracy=BEST_ACCURACY
            )
            checkpointer.write_to_disk("ckpt_calibrated.pth")
            logger.info(f"calibrated checkpoint has been wrote to disk")

        img = img.to(DEVICE, non_blocking=True)
        label = label.to(DEVICE, non_blocking=True)
        logits = model(img, enable_fp=CONF.quant.enable_fp, enable_quant=scheduler.quant_enabled)

        if teacher_model is not None:
            with torch.no_grad():
                teacher_logits = teacher_model(img)
        else:
            teacher_logits = None

        if CONF.distil.mode == "distil":
            hard_loss, soft_loss = criterion(logits, teacher_logits, label)
            loss = soft_loss + hard_loss
        elif CONF.distil.mode == "inv_distil":
            hard_loss, soft_loss, ref_loss = criterion(*logits, label, teacher_logits)
            hard_w, soft_w, ref_w = scheduler.get_scheduled_variables("hard_w", "soft_w", "ref_w")
            loss = hard_loss * hard_w + soft_loss * soft_w + ref_loss * ref_w
        else:
            if scheduler.quant_enabled:
                logit = logits[1]
            else:
                logit = logits[0]
            loss = criterion(logit, label)

        opt.zero_grad()
        loss.backward()
        opt.step(scheduler.quant_enabled)

        (fp_top1, fp_top5), (q_top1, q_top5) = accuracy(logits, label, topk=CONF.loss.topk)

        n = img.size(0)
        metric_logger.update(
            train_fp_top1=(fp_top1, n),
            train_fp_top5=(fp_top5, n),
            train_q_top1=(q_top1, n),
            train_q_top5=(q_top5, n),
            LR=opt.get_lr()[0],
            loss=(loss.item(), n),
        )

        if step % CONF.eval.freq == 0 or step == len(train_loader):  # step starts from 1
            logger.debug(f"evaluating at iteration {step}...")
            eval_fp_top1, eval_fp_top5, eval_q_top1, eval_q_top5 = evaluate(
                model,
                val_loader,
                enable_quant=scheduler.quant_enabled,
                verbose=True,
                progress_bar=CONF.progress_bar
            )
            metric_logger.update(
                eval_fp_top1=(eval_fp_top1, 1),
                eval_fp_top5=(eval_fp_top5, 1),
                eval_q_top1=(eval_q_top1, 1),
                eval_q_top5=(eval_q_top5, 1)
            )

            is_best = eval_q_top1 > BEST_ACCURACY if scheduler.quant_enabled else eval_fp_top1 > BEST_ACCURACY
            if is_best:
                BEST_ACCURACY = eval_q_top1 if scheduler.quant_enabled else eval_fp_top1
                checkpointer.save(
                    model=model_without_ddp.state_dict(),
                    opt=opt.state_dict(),
                    scheduler=scheduler.state_dict(),
                    accuracy=BEST_ACCURACY
                )
            dist.barrier()

        if step % CONF.ckpt.freq == 0:
            checkpointer.write_to_disk(f"ckpt_step_{step}.pth")
            dist.barrier()

    checkpointer.write_to_disk(f"ckpt_final.pth")
    dist.barrier()


@torch.no_grad()
def evaluate(model, loader, enable_fp=True, enable_quant=True, verbose=False, progress_bar=True, use_ema_stat=True):
    if use_ema_stat:
        model.eval()
    metric_logger = MetricLogger(track_global_stat=True)

    if progress_bar:
        loader = tqdm(loader, f"[RANK {dist.get_rank():2d}]")

    for img, label in loader:
        n = img.size(0)
        img = img.to(DEVICE, non_blocking=True)
        label = label.to(DEVICE, non_blocking=True)
        logits = model(img, enable_fp=enable_fp, enable_quant=enable_quant)
        (fp_top1, fp_top5), (q_top1, q_top5) = accuracy(logits, label, topk=CONF.loss.topk)
        metric_logger.update(
            eval_fp_top1=(fp_top1, n),
            eval_fp_top5=(fp_top5, n),
            eval_q_top1=(q_top1, n),
            eval_q_top5=(q_top5, n),
        )

    model.train()
    metric_logger.synchronize_between_processes()

    if verbose:
        logger = logging.getLogger(LOGGER_NAME)
        logger.info(f"{str(metric_logger)}")

    return metric_logger.get_meter("eval_fp_top1", "eval_fp_top5", "eval_q_top1", "eval_q_top5")


if __name__ == "__main__":
    if mp.get_start_method(allow_none=True) != "forkserver":
        mp.set_start_method("forkserver")

    main()
