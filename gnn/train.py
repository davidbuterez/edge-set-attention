import argparse
import copy
import os
import sys
import torch
import wandb
import numpy as np
import pytorch_lightning as pl

from pathlib import Path
from torch_geometric.seed import seed_everything
from torch_geometric.loader import DataLoader
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping
from pytorch_lightning.loggers import WandbLogger

# Imports from this project
sys.path.append(os.path.realpath("."))

from gnn.graph_models import Estimator
from data_loading.data_loading import get_dataset_train_val_test
from gnn.config import (
    save_gnn_arguments_to_json,
    load_gnn_arguments_from_json,
    validate_gnn_argparse_arguments,
    get_gnn_wandb_name,
)

os.environ["WANDB__SERVICE_WAIT"] = "500"

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True


def check_is_node_level_dataset(dataset_name):
    if dataset_name in ["PPI", "Cora", "CiteSeer"]:
        return True
    elif "infected" in dataset_name:
        return True
    elif "hetero" in dataset_name:
        return True
    
    return False


def main():
    torch.set_num_threads(1)

    parser = argparse.ArgumentParser()

    # Seed for seed_everything
    parser.add_argument("--seed", type=int)

    # Dataset arguments
    parser.add_argument("--dataset", type=str)
    parser.add_argument("--dataset-download-dir", type=str)
    parser.add_argument("--dataset-one-hot", default=True, action=argparse.BooleanOptionalAction)
    parser.add_argument("--dataset-target-name", type=str)

    # GNN arguments
    parser.add_argument("--output-node-dim", type=int, default=256)
    parser.add_argument("--num-layers", type=int, default=4)
    parser.add_argument("--conv-type", choices=["GCN", "GIN", "PNA", "GAT", "GATv2", "GINDrop"])
    parser.add_argument("--gnn-intermediate-dim", type=int, default=256)
    parser.add_argument("--gat-attn-heads", type=int, default=0)
    parser.add_argument("--gat-dropout", type=float, default=0)

    # Learning hyperparameters
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float)
    parser.add_argument("--monitor-loss-name", type=str)
    parser.add_argument("--gradient-clip-val", type=float, default=0.5)
    parser.add_argument("--optimiser-weight-decay", type=float, default=1e-3)
    parser.add_argument("--regression-loss-fn", type=str, choices=["mae", "mse"])
    parser.add_argument("--early-stopping-patience", type=int, default=30)
    parser.add_argument("--train-regime", type=str, choices=["gpu-32", "gpu-bf16", "gpu-fp16", "cpu"])

    # Path/config arguments
    parser.add_argument("--ckpt-path", type=str)
    parser.add_argument("--out-path", type=str)
    parser.add_argument("--config-json-path", type=str)
    parser.add_argument("--wandb-project-name", type=str)

    args = parser.parse_args()

    if args.config_json_path:
        argsdict = load_gnn_arguments_from_json(args.config_json_path)
        validate_gnn_argparse_arguments(argsdict)
    else:
        argsdict = vars(args)
        validate_gnn_argparse_arguments(argsdict)
        del argsdict["config_json_path"]

    seed_everything(argsdict["seed"])

    # Dataset arguments
    dataset = argsdict["dataset"]
    dataset_download_dir = argsdict["dataset_download_dir"]
    dataset_one_hot = argsdict["dataset_one_hot"]
    target_name = argsdict["dataset_target_name"]

    # Learning hyperparameters
    batch_size = argsdict["batch_size"]
    early_stopping_patience = argsdict["early_stopping_patience"]
    gradient_clip_val = argsdict["gradient_clip_val"]
    monitor_loss_name = argsdict["monitor_loss_name"]
    regr_fn = argsdict["regression_loss_fn"]

    # Path/config arguments
    ckpt_path = argsdict["ckpt_path"]
    out_path = argsdict["out_path"]
    wandb_project_name = argsdict["wandb_project_name"]
    train_regime = argsdict["train_regime"]

    if monitor_loss_name == "MCC" or "MCC" in monitor_loss_name:
        monitor_loss_name = "Validation MCC"

    if dataset in ["ESOL", "FreeSolv", "Lipo", "QM9", "DOCKSTRING", "ZINC", "PCQM4Mv2", "lrgb-pept-struct"]:
        assert regr_fn is not None, "A loss functions must be specified for regression tasks!"

    if dataset in ["QM9", "DOCKSTRING"]:
        assert target_name is not None, "A target must be specified for QM9 and DOCKSTRING!"

    ############## Data loading ##############
    train_mask, val_mask, test_mask = None, None, None

    if check_is_node_level_dataset(dataset):
        # Node-level task branch
        train, val, test, num_classes, task_type, scaler, train_mask, val_mask, test_mask = get_dataset_train_val_test(
            dataset=dataset,
            dataset_dir=dataset_download_dir,
        )
    else:
        # Graph-level task branch
        train, val, test, num_classes, task_type, scaler = get_dataset_train_val_test(
            dataset=dataset,
            dataset_dir=dataset_download_dir,
            one_hot=dataset_one_hot,
            target_name=target_name,
        )

    if len(train) % batch_size == 1:
        batch_size += 1

    num_features = train[0].x.shape[-1]
    edge_dim = None
    if hasattr(train[0], "edge_attr") and train[0].edge_attr is not None:
        edge_dim = train[0].edge_attr.shape[-1]

    train_loader = DataLoader(train, batch_size=batch_size, shuffle=True, num_workers=4, pin_memory=False)
    val_loader = DataLoader(val, batch_size=batch_size, shuffle=False, num_workers=4, pin_memory=False)
    test_loader = DataLoader(test, batch_size=batch_size, shuffle=False, num_workers=4, pin_memory=False)
    ############## Data loading ##############

    run_name = get_gnn_wandb_name(argsdict)

    output_save_dir = os.path.join(out_path, run_name)
    Path(output_save_dir).mkdir(exist_ok=True, parents=True)

    config_json_path = save_gnn_arguments_to_json(argsdict, output_save_dir)

    # Logging
    logger = WandbLogger(name=run_name, project=wandb_project_name)

    # Callbacks
    monitor_mode = "max" if "MCC" in monitor_loss_name else "min"
    checkpoint_callback = ModelCheckpoint(
        monitor=monitor_loss_name,
        dirpath=output_save_dir,
        filename="{epoch:03d}",
        mode=monitor_mode,
        save_top_k=1,
    )

    early_stopping_callback = EarlyStopping(
        monitor=monitor_loss_name, patience=early_stopping_patience, mode=monitor_mode
    )

    ############## Learning and model set-up ##############
    gnn_args = copy.deepcopy(argsdict)
    gnn_args = gnn_args | dict(
        task_type=task_type,
        num_features=num_features,
        linear_output_size=num_classes,
        scaler=scaler,
        edge_dim=edge_dim,
        out_path=output_save_dir,
        use_cpu=train_regime == "cpu",
        train_mask=train_mask,
        val_mask=val_mask,
        test_mask=test_mask,
    )

    if argsdict["conv_type"] == "PNA":
        gnn_args = gnn_args | dict(train_dataset_for_PNA=train)

    model = Estimator(**gnn_args)

    if train_regime == "gpu-bf16":
        precision = "bf16-mixed"
    elif train_regime == "gpu-fp16":
        precision = "16-mixed"
    else:
        precision = "32"

    trainer_args = dict(
        callbacks=[checkpoint_callback, early_stopping_callback],
        logger=logger,
        min_epochs=10,
        max_epochs=-1,
        devices=1,
        check_val_every_n_epoch=1,
        num_sanity_val_steps=0,
        precision=precision,
        gradient_clip_val=gradient_clip_val,
    )

    if "gpu" in train_regime:
        trainer_args = trainer_args | dict(accelerator="gpu")
    else:
        trainer_args = trainer_args | dict(accelerator="cpu")
    ############## Learning and model set-up ##############

    trainer = pl.Trainer(**trainer_args)

    trainer.fit(
        model=model, train_dataloaders=train_loader, val_dataloaders=[val_loader, test_loader], ckpt_path=ckpt_path
    )
    trainer.test(model=model, dataloaders=test_loader, ckpt_path="best")

    # Save test metrics
    preds_path = os.path.join(output_save_dir, "test_y_pred.npy")
    true_path = os.path.join(output_save_dir, "test_y_true.npy")
    metrics_path = os.path.join(output_save_dir, "test_metrics.npy")

    np.save(preds_path, model.test_output)
    np.save(true_path, model.test_true)
    np.save(metrics_path, model.test_metrics)

    wandb.save(preds_path)
    wandb.save(true_path)
    wandb.save(metrics_path)
    wandb.save(config_json_path)

    # ckpt_paths = [str(p) for p in Path(output_save_dir).rglob("*.ckpt")]
    # for cp in ckpt_paths:
    #     wandb.save(cp)

    wandb.finish()


if __name__ == "__main__":
    main()
