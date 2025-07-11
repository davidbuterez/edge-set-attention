import logging
import time

import numpy as np
import torch
import torch.nn.functional as F

from pathlib import Path
from torch_geometric.graphgym.checkpoint import load_ckpt, save_ckpt, clean_ckpt
from torch_geometric.graphgym.config import cfg
from torch_geometric.graphgym.loss import compute_loss
from torch_geometric.graphgym.register import register_train
from torch_geometric.graphgym.utils.epoch import is_eval_epoch, is_ckpt_epoch

from graphgps.loss.subtoken_prediction_loss import subtoken_cross_entropy
from graphgps.utils import cfg_to_dict, flatten_dict, make_wandb_name


def arxiv_cross_entropy(pred, true, split_idx):
    true = true.squeeze(-1)
    if pred.ndim > 1 and true.ndim == 1:
        pred_score = F.log_softmax(pred[split_idx], dim=-1)
        loss = F.nll_loss(pred_score, true[split_idx])
    else:
        raise ValueError("In ogbn cross_entropy calculation dimensions did not match.")
    return loss, pred_score


def train_epoch(logger, loader, model, optimizer, grad_scaler, scheduler, batch_accumulation, scaler=None):
    model.train()
    # optimizer.zero_grad()
    time_start = time.time()
    for iter, batch in enumerate(loader):
        batch.split = "train"
        batch.to(torch.device(cfg.device))

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            pred, true = model(batch)

            if cfg.dataset.name == "ogbg-code2":
                loss, pred_score = subtoken_cross_entropy(pred, true)
                _true = true
                _pred = pred_score
            elif cfg.dataset.name == "ogbn-arxiv":
                split_idx = loader.dataset.split_idx["train"].to(torch.device(cfg.device))
                loss, pred_score = arxiv_cross_entropy(pred, true, split_idx)
                _true = true[split_idx].detach().to("cpu", non_blocking=True)
                _pred = pred_score.detach().to("cpu", non_blocking=True)
            else:
                loss, pred_score = compute_loss(pred, true)
                _true = true.detach().to("cpu", non_blocking=True)
                _pred = pred_score.detach().to("cpu", non_blocking=True)
            # loss.backward()
            grad_scaler.scale(loss).backward()
            # grad_scaler.unscale_(optimizer)
            # Parameters update after accumulating gradients for given num. batches.
            if ((iter + 1) % batch_accumulation == 0) or (iter + 1 == len(loader)):
                if cfg.optim.clip_grad_norm:
                    grad_scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

                grad_scaler.step(optimizer)
                grad_scaler.update()
                optimizer.zero_grad() # set_to_none=True here can modestly improve performance
                # optimizer.step()
                # optimizer.zero_grad()

        if cfg.dataset.task_type == "regression":
            _pred = torch.from_numpy(scaler.inverse_transform(_pred.float().reshape(-1, 1).detach().cpu().numpy()))
            _true = torch.from_numpy(scaler.inverse_transform(_true.float().reshape(-1, 1).detach().cpu().numpy()))

        logger.update_stats(
            true=_true,
            pred=_pred,
            loss=loss.detach().cpu().item(),
            lr=scheduler.get_last_lr()[0],
            time_used=time.time() - time_start,
            params=cfg.params,
            dataset_name=cfg.dataset.name,
        )
        time_start = time.time()


@torch.no_grad()
def eval_epoch(logger, loader, model, split="val", scaler=None):
    model.eval()
    time_start = time.time()
    for batch in loader:
        batch.split = split
        batch.to(torch.device(cfg.device))

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            if cfg.gnn.head == "inductive_edge":
                pred, true, extra_stats = model(batch)
            else:
                pred, true = model(batch)
                extra_stats = {}

            if cfg.dataset.name == "ogbg-code2":
                loss, pred_score = subtoken_cross_entropy(pred, true)
                _true = true
                _pred = pred_score
            elif cfg.dataset.name == "ogbn-arxiv":
                index_split = loader.dataset.split_idx[split].to(torch.device(cfg.device))
                loss, pred_score = arxiv_cross_entropy(pred, true, index_split)
                _true = true[index_split].detach().to("cpu", non_blocking=True)
                _pred = pred_score.detach().to("cpu", non_blocking=True)
            else:
                loss, pred_score = compute_loss(pred, true)
                _true = true.detach().to("cpu", non_blocking=True)
                _pred = pred_score.detach().to("cpu", non_blocking=True)

        if cfg.dataset.task_type == "regression":
            _pred = torch.from_numpy(scaler.inverse_transform(_pred.float().reshape(-1, 1).detach().cpu().numpy()))
            _true = torch.from_numpy(scaler.inverse_transform(_true.float().reshape(-1, 1).detach().cpu().numpy()))

        logger.update_stats(
            true=_true,
            pred=_pred,
            loss=loss.detach().cpu().item(),
            lr=0,
            time_used=time.time() - time_start,
            params=cfg.params,
            dataset_name=cfg.dataset.name,
            **extra_stats,
        )
        time_start = time.time()


@register_train("custom")
def custom_train(loggers, loaders, model, optimizer, scheduler, scaler):
    """
    Customized training pipeline.

    Args:
        loggers: List of loggers
        loaders: List of loaders
        model: GNN model
        optimizer: PyTorch optimizer
        scheduler: PyTorch learning rate scheduler

    """
    start_epoch = 0
    if cfg.train.auto_resume:
        start_epoch = load_ckpt(model, optimizer, scheduler, cfg.train.epoch_resume)
    if start_epoch == cfg.optim.max_epoch:
        logging.info("Checkpoint found, Task already done")
    else:
        logging.info("Start from epoch %s", start_epoch)

    print('Using early_stopping_patience = ', cfg.optim.early_stopping_patience)

    if cfg.wandb.use:
        try:
            import wandb
        except:
            raise ImportError("WandB is not installed.")
        if cfg.wandb.name == "":
            wandb_name = make_wandb_name(cfg)
        else:
            wandb_name = cfg.wandb.name
        run = wandb.init(entity=cfg.wandb.entity, project=cfg.wandb.project, name=wandb_name)
        run.config.update(cfg_to_dict(cfg))

    num_splits = len(loggers)
    split_names = ["val", "test"]
    full_epoch_times = []
    perf = [[] for _ in range(num_splits)]

    grad_scaler = torch.amp.GradScaler()

    for cur_epoch in range(start_epoch, cfg.optim.max_epoch):
        start_time = time.perf_counter()
        
        train_epoch(loggers[0], loaders[0], model, optimizer, grad_scaler, scheduler, cfg.optim.batch_accumulation, scaler)
        perf[0].append(loggers[0].write_epoch(cur_epoch))
        if is_eval_epoch(cur_epoch):
            for i in range(1, num_splits):
                eval_epoch(loggers[i], loaders[i], model, split=split_names[i - 1], scaler=scaler)
                perf[i].append(loggers[i].write_epoch(cur_epoch))
        else:
            for i in range(1, num_splits):
                perf[i].append(perf[i][-1])

        val_perf = perf[1]
        print('val_perf = ', val_perf)
        if cfg.optim.scheduler == "reduce_on_plateau":
            scheduler.step(val_perf[-1]["loss"])
        else:
            scheduler.step()
        full_epoch_times.append(time.perf_counter() - start_time)
        # Checkpoint with regular frequency (if enabled).
        if cfg.train.enable_ckpt and not cfg.train.ckpt_best and is_ckpt_epoch(cur_epoch):
            save_ckpt(model, optimizer, scheduler, cur_epoch)

        if cfg.wandb.use:
            run.log(flatten_dict(perf), step=cur_epoch)

        # Log current best stats on eval epoch.
        if is_eval_epoch(cur_epoch):
            best_epoch = np.array([float(vp["loss"]) for vp in val_perf]).argmin()
            best_train = best_val = best_test = ""
            m = cfg.metric_best
            if cfg.metric_best != "auto":
                # Select again based on val perf of `cfg.metric_best`.
                best_epoch = getattr(np.array([vp[m] for vp in val_perf]), cfg.metric_agg)()
                if m in perf[0][best_epoch]:
                    best_train = f"train_{m}: {perf[0][best_epoch][m]:.4f}"
                else:
                    # Note: For some datasets it is too expensive to compute
                    # the main metric on the training set.
                    best_train = f"train_{m}: {0:.4f}"
                best_val = f"val_{m}: {perf[1][best_epoch][m]:.4f}"
                best_test = f"test_{m}: {perf[2][best_epoch][m]:.4f}"

            if cfg.wandb.use:
                bstats = {"best/epoch": best_epoch}
                for i, s in enumerate(["train", "val", "test"]):
                    bstats[f"best/{s}_loss"] = perf[i][best_epoch]["loss"]
                    if m in perf[i][best_epoch]:
                        bstats[f"best/{s}_{m}"] = perf[i][best_epoch][m]
                        run.summary[f"best_{s}_perf"] = perf[i][best_epoch][m]
                    for x in ["hits@1", "hits@3", "hits@10", "mrr"]:
                        if x in perf[i][best_epoch]:
                            bstats[f"best/{s}_{x}"] = perf[i][best_epoch][x]
                run.log(bstats, step=cur_epoch)
                run.summary["full_epoch_time_avg"] = np.mean(full_epoch_times)
                run.summary["full_epoch_time_sum"] = np.sum(full_epoch_times)
        # Checkpoint the best epoch params (if enabled).
        if cfg.train.enable_ckpt and cfg.train.ckpt_best and best_epoch == cur_epoch:
            save_ckpt(model, optimizer, scheduler, cur_epoch)
            if cfg.train.ckpt_clean:  # Delete old ckpt each time.
                clean_ckpt()
        logging.info(
            f"> Epoch {cur_epoch}: took {full_epoch_times[-1]:.1f}s "
            f"(avg {np.mean(full_epoch_times):.1f}s) | "
            f"Best so far: epoch {best_epoch}\t"
            f"train_loss: {perf[0][best_epoch]['loss']:.4f} {best_train}\t"
            f"val_loss: {perf[1][best_epoch]['loss']:.4f} {best_val}\t"
            f"test_loss: {perf[2][best_epoch]['loss']:.4f} {best_test}"
        )
        if hasattr(model, "trf_layers"):
            # Log SAN's gamma parameter values if they are trainable.
            for li, gtl in enumerate(model.trf_layers):
                if torch.is_tensor(gtl.attention.gamma) and gtl.attention.gamma.requires_grad:
                    logging.info(f"    {gtl.__class__.__name__} {li}: " f"gamma={gtl.attention.gamma.item()}")

        
        if best_epoch > -1 and cur_epoch - best_epoch > cfg.optim.early_stopping_patience:
            print(f'Stopping early at epoch {cur_epoch}!')
            break
    logging.info(f"Avg time per epoch: {np.mean(full_epoch_times):.2f}s")
    logging.info(f"Total train loop time: {np.sum(full_epoch_times) / 3600:.2f}h")

    for logger in loggers:
        logger.close()
    if cfg.train.ckpt_clean:
        clean_ckpt()

    # Upload remaining checkpoint to wandb
    # ckpt_paths = [str(p) for p in Path(cfg.out_dir).rglob("*.ckpt")]
    # for cp in ckpt_paths:
    #     wandb.save(cp)

    # Close wandb
    if cfg.wandb.use:
        run.finish()
        run = None

    logging.info("Task done, results saved in %s", cfg.run_dir)


@register_train("inference-only")
def inference_only(loggers, loaders, model, optimizer=None, scheduler=None):
    """
    Customized pipeline to run inference only.

    Args:
        loggers: List of loggers
        loaders: List of loaders
        model: GNN model
        optimizer: Unused, exists just for API compatibility
        scheduler: Unused, exists just for API compatibility
    """
    num_splits = len(loggers)
    split_names = ["train", "val", "test"]
    perf = [[] for _ in range(num_splits)]
    cur_epoch = 0
    start_time = time.perf_counter()

    for i in range(0, num_splits):
        eval_epoch(loggers[i], loaders[i], model, split=split_names[i])
        perf[i].append(loggers[i].write_epoch(cur_epoch))
    val_perf = perf[1]

    best_epoch = np.array([vp["loss"] for vp in val_perf]).argmin()
    best_train = best_val = best_test = ""
    if cfg.metric_best != "auto":
        # Select again based on val perf of `cfg.metric_best`.
        m = cfg.metric_best
        best_epoch = getattr(np.array([vp[m] for vp in val_perf]), cfg.metric_agg)()
        if m in perf[0][best_epoch]:
            best_train = f"train_{m}: {perf[0][best_epoch][m]:.4f}"
        else:
            # Note: For some datasets it is too expensive to compute
            # the main metric on the training set.
            best_train = f"train_{m}: {0:.4f}"
        best_val = f"val_{m}: {perf[1][best_epoch][m]:.4f}"
        best_test = f"test_{m}: {perf[2][best_epoch][m]:.4f}"

    logging.info(
        f"> Inference | "
        f"train_loss: {perf[0][best_epoch]['loss']:.4f} {best_train}\t"
        f"val_loss: {perf[1][best_epoch]['loss']:.4f} {best_val}\t"
        f"test_loss: {perf[2][best_epoch]['loss']:.4f} {best_test}"
    )
    logging.info(f"Done! took: {time.perf_counter() - start_time:.2f}s")
    for logger in loggers:
        logger.close()
