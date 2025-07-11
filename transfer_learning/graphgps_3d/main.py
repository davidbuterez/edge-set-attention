import datetime
import os
import torch
import logging

import graphgps  # noqa, register custom modules
from graphgps.optimizer.extra_optimizers import ExtendedSchedulerConfig

from torch_geometric.graphgym.cmd_args import parse_args
from torch_geometric.graphgym.config import cfg, dump_cfg, set_cfg, load_cfg, makedirs_rm_exist

# from torch_geometric.graphgym.loader import create_loader
# from data_loading.graphgps_utils import create_loader
from torch_geometric.graphgym.logger import set_printing
from torch_geometric.graphgym.optim import create_optimizer, create_scheduler, OptimizerConfig

# from torch_geometric.graphgym.model_builder import create_model
from torch_geometric.graphgym.model_builder import GraphGymModule
from torch_geometric.graphgym.train import train
from torch_geometric.graphgym.utils.agg_runs import agg_runs
from torch_geometric.graphgym.utils.comp_budget import params_count
from torch_geometric.graphgym.utils.device import auto_select_device
from torch_geometric.graphgym.register import train_dict
from torch_geometric import seed_everything

from graphgps.finetuning import load_pretrained_model_cfg, init_model_from_pretrained

# from graphgps.logger import create_logger
from graphgps.logger import CustomLogger

from torch_geometric.graphgym.loader import get_loader  # , set_dataset_info
from graphgps.loader.master_loader import load_dataset_master


torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True


def set_dataset_info(dataset):
    r"""
    Set global dataset information

    Args:
        dataset: PyG dataset object

    """

    # get dim_in and dim_out
    try:
        cfg.share.dim_in = dataset._data.x.shape[1]
    except Exception:
        cfg.share.dim_in = 1

    # count number of dataset splits
    cfg.share.num_splits = 1
    for key in dataset._data.keys():
        if "val" in key:
            cfg.share.num_splits += 1
            break
    for key in dataset._data.keys():
        if "test" in key:
            cfg.share.num_splits += 1
            break


def create_dataset():
    r"""
    Create dataset object

    Returns: PyG dataset object

    """

    format = cfg.dataset.format
    name = cfg.dataset.name
    dataset_dir = cfg.dataset.dir

    dataset, scaler = load_dataset_master(format, name, dataset_dir)
    set_dataset_info(dataset)

    return dataset, scaler


def create_loader():
    """
    Create data loader object

    Returns: List of PyTorch data loaders

    """
    dataset, scaler = create_dataset()
    # train loader
    if cfg.dataset.task == "graph":
        id = dataset.data["train_graph_index"]
        loaders = [get_loader(dataset[id], cfg.train.sampler, cfg.train.batch_size, shuffle=True)]
        delattr(dataset.data, "train_graph_index")
    else:
        loaders = [get_loader(dataset, cfg.train.sampler, cfg.train.batch_size, shuffle=True)]

    # val and test loaders
    for i in range(cfg.share.num_splits - 1):
        if cfg.dataset.task == "graph":
            split_names = ["val_graph_index", "test_graph_index"]
            id = dataset.data[split_names[i]]
            loaders.append(get_loader(dataset[id], cfg.val.sampler, cfg.train.batch_size, shuffle=False))
            delattr(dataset.data, split_names[i])
        else:
            loaders.append(get_loader(dataset, cfg.val.sampler, cfg.train.batch_size, shuffle=False))

    return loaders, scaler


def create_model(to_device=True, dim_in=None, dim_out=None):
    dim_in = cfg.share.dim_in if dim_in is None else dim_in
    dim_in = cfg.gnn.dim_inner
    dim_out = cfg.share.dim_out if dim_out is None else dim_out

    model = GraphGymModule(dim_in, dim_out, cfg)
    if to_device:
        model.to(torch.device(cfg.accelerator))
    return model


def create_logger():
    r"""Create logger for the experiment."""
    loggers = []
    names = ["train", "val", "test"]
    for i, dataset in enumerate(range(cfg.share.num_splits)):
        loggers.append(CustomLogger(name=names[i], task_type=cfg.dataset.task_type))
    return loggers


def new_optimizer_config(cfg):
    return OptimizerConfig(
        optimizer=cfg.optim.optimizer,
        base_lr=cfg.optim.base_lr,
        weight_decay=cfg.optim.weight_decay,
        momentum=cfg.optim.momentum,
    )


def new_scheduler_config(cfg):
    return ExtendedSchedulerConfig(
        scheduler=cfg.optim.scheduler,
        steps=cfg.optim.steps,
        lr_decay=cfg.optim.lr_decay,
        max_epoch=cfg.optim.max_epoch,
        reduce_factor=cfg.optim.reduce_factor,
        schedule_patience=cfg.optim.schedule_patience,
        min_lr=cfg.optim.min_lr,
        num_warmup_epochs=cfg.optim.num_warmup_epochs,
        train_mode=cfg.train.mode,
        eval_period=cfg.train.eval_period,
    )


def custom_set_out_dir(cfg, cfg_fname, name_tag):
    """Set custom main output directory path to cfg.
    Include the config filename and name_tag in the new :obj:`cfg.out_dir`.

    Args:
        cfg (CfgNode): Configuration node
        cfg_fname (string): Filename for the yaml format configuration file
        name_tag (string): Additional name tag to identify this execution of the
            configuration file, specified in :obj:`cfg.name_tag`
    """
    run_name = os.path.splitext(os.path.basename(cfg_fname))[0]
    run_name += f"-{name_tag}" if name_tag else ""
    cfg.out_dir = os.path.join(cfg.out_dir, run_name)


def custom_set_run_dir(cfg, run_id):
    """Custom output directory naming for each experiment run.

    Args:
        cfg (CfgNode): Configuration node
        run_id (int): Main for-loop iter id (the random seed or dataset split)
    """
    cfg.run_dir = os.path.join(cfg.out_dir, str(run_id))
    # Make output directory
    if cfg.train.auto_resume:
        os.makedirs(cfg.run_dir, exist_ok=True)
    else:
        makedirs_rm_exist(cfg.run_dir)


def run_loop_settings():
    """Create main loop execution settings based on the current cfg.

    Configures the main execution loop to run in one of two modes:
    1. 'multi-seed' - Reproduces default behaviour of GraphGym when
        args.repeats controls how many times the experiment run is repeated.
        Each iteration is executed with a random seed set to an increment from
        the previous one, starting at initial cfg.seed.
    2. 'multi-split' - Executes the experiment run over multiple dataset splits,
        these can be multiple CV splits or multiple standard splits. The random
        seed is reset to the initial cfg.seed value for each run iteration.

    Returns:
        List of run IDs for each loop iteration
        List of rng seeds to loop over
        List of dataset split indices to loop over
    """
    if len(cfg.run_multiple_splits) == 0:
        # 'multi-seed' run mode
        num_iterations = args.repeat
        seeds = [cfg.seed + x for x in range(num_iterations)]
        split_indices = [cfg.dataset.split_index] * num_iterations
        run_ids = seeds
    else:
        # 'multi-split' run mode
        if args.repeat != 1:
            raise NotImplementedError("Running multiple repeats of multiple " "splits in one run is not supported.")
        num_iterations = len(cfg.run_multiple_splits)
        seeds = [cfg.seed] * num_iterations
        split_indices = cfg.run_multiple_splits
        run_ids = split_indices
    return run_ids, seeds, split_indices


if __name__ == "__main__":
    # Load cmd line args
    args = parse_args()
    # Load config file
    set_cfg(cfg)
    load_cfg(cfg, args)
    custom_set_out_dir(cfg, args.cfg_file, cfg.name_tag)
    dump_cfg(cfg)
    # Set Pytorch environment
    # torch.set_num_threads(cfg.num_threads)
    torch.set_num_threads(1)
    # Repeat for multiple experiment runs
    for run_id, seed, split_index in zip(*run_loop_settings()):
        # Set configurations for each run
        custom_set_run_dir(cfg, run_id)
        set_printing()
        cfg.dataset.split_index = split_index
        cfg.seed = seed
        cfg.run_id = run_id
        seed_everything(cfg.seed)
        auto_select_device()
        if cfg.pretrained.dir:
            cfg = load_pretrained_model_cfg(cfg)
        logging.info(f"[*] Run ID {run_id}: seed={cfg.seed}, " f"split_index={cfg.dataset.split_index}")
        logging.info(f"    Starting now: {datetime.datetime.now()}")
        # Set machine learning pipeline
        loaders, scaler = create_loader()
        loggers = create_logger()
        # custom_train expects three loggers for 'train', 'valid' and 'test'.
        # GraphGym code creates one logger/loader for each of the 'train_mask' etc.
        # attributes in the dataset. As a work around it, we create one logger for each
        # of the types.
        # loaders are a const, so it is ok to just duplicate the loader.
        if cfg.dataset.name == "ogbn-arxiv" or cfg.dataset.name == "ogbn-proteins":
            loggers_2 = create_logger()
            loggers_3 = create_logger()
            loggers_2[0].name = "val"
            loggers_3[0].name = "test"
            loggers.extend(loggers_2)
            loggers.extend(loggers_3)
            loaders = loaders * 3
        model = create_model()
        if cfg.pretrained.dir:
            model = init_model_from_pretrained(
                model, cfg.pretrained.dir, cfg.pretrained.freeze_main, cfg.pretrained.reset_prediction_head
            )
        
        optimizer = create_optimizer(model.parameters(), new_optimizer_config(cfg))
        scheduler = create_scheduler(optimizer, new_scheduler_config(cfg))
        # Print model info
        logging.info(model)
        logging.info(cfg)
        cfg.params = params_count(model)
        logging.info("Num parameters: %s", cfg.params)
        # Start training
        if cfg.train.mode == "standard":
            raise NotImplementedError
            # if cfg.wandb.use:
            #     logging.warning("[W] WandB logging is not supported with the "
            #                     "default train.mode, set it to `custom`")
            # train(loggers, loaders, model, optimizer, scheduler)
        else:
            # with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
            train_dict[cfg.train.mode](loggers, loaders, model, optimizer, scheduler, scaler)
    # Aggregate results from different seeds
    try:
        agg_runs(cfg.out_dir, cfg.metric_best)
    except Exception as e:
        logging.info(f"Failed when trying to aggregate multiple runs: {e}")
    # When being launched in batch mode, mark a yaml as done
    if args.mark_done:
        os.rename(args.cfg_file, f"{args.cfg_file}_done")
    logging.info(f"[*] All done: {datetime.datetime.now()}")
