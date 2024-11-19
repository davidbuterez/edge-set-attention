# Copyright (c) Microsoft Corporation and HuggingFace
# Licensed under the MIT License.

from typing import Any, Dict, List, Mapping

import numpy as np
import torch
import torch.nn.functional as F

from transformers.utils.import_utils import is_cython_available


MOLECULAR_DATASETS = [
    "QM9",
    "DOCKSTRING",
    "ESOL",
    "FreeSolv",
    "Lipo",
    "PCBA",
    "MUV",
    "HIV",
    "BACE",
    "BBBP",
    "Tox21",
    "ToxCast",
    "ZINC",
    "SIDER",
    "ClinTox",
]


if is_cython_available():
    import pyximport

    pyximport.install(setup_args={"include_dirs": np.get_include()})
    from . import algos_tokengt  # noqa E402


# From algos.py
def eig(sym_mat):
    # (sorted) eigenvectors with numpy
    EigVal, EigVec = np.linalg.eigh(sym_mat)

    # for eigval, take abs because numpy sometimes computes the first eigenvalue approaching 0 from the negative
    eigvec = EigVec.astype(dtype=np.single)  # [num_nodes, num_nodes (channels)]
    eigval = np.sort(np.abs(np.real(EigVal))).astype(dtype=np.single)  # [num_nodes (channels),]
    return eigvec, eigval  # [num_nodes, num_nodes (channels)]  [num_nodes (channels),]


def lap_eig(dense_adj, number_of_nodes, in_degree):
    """
    Graph positional encoding v/ Laplacian eigenvectors https://github.com/DevinKreuzer/SAN/blob/main/data/molecules.py
    """
    # Laplacian
    A = dense_adj.astype(dtype=np.single)
    num_nodes = np.diag(in_degree.astype(dtype=np.single).clip(1) ** -0.5)
    L = np.eye(number_of_nodes) - num_nodes @ A @ num_nodes

    eigvec, eigval = eig(L)
    return eigvec, eigval  # [num_nodes, num_nodes (channels)]  [num_nodes (channels),]


# From wrapper.py
def convert_to_single_emb(x, offset: int = 512):
    feature_num = x.shape[1] if len(x.shape) > 1 else 1
    feature_offset = 1 + np.arange(0, feature_num * offset, offset, dtype=np.int32)
    x = x + feature_offset
    return x


def convert_to_single_emb_first_then_same(x, first_offset: int = 120, other_offset: int = 10):
    feature_num = x.shape[1] if len(x.shape) > 1 else 1
    feature_offsets = np.zeros(feature_num, dtype=np.int32)
    feature_offsets[0] = 1  # Start with an offset for the first feature
    feature_offsets[1:] = first_offset + (np.arange(1, feature_num) * other_offset)
    x = x + feature_offsets
    return x


def convert_to_single_emb_node(x, dataset_name, max_degree):
    # For ChemProp features, we want only the first feature (atom type) to have a large offset
    if dataset_name in MOLECULAR_DATASETS:
        input_nodes = convert_to_single_emb_first_then_same(x, first_offset=120, other_offset=10) + 1
    else:
        # Otherwise we most likely have a degree integer and we must use it as a large offset
        if max_degree:
            input_nodes = convert_to_single_emb(x, offset=max_degree) + 1
        # Otherwise we most likely have pre-defined one-hot node features. Add a small offset
        else:
            input_nodes = convert_to_single_emb(x, offset=2) + 1

    return input_nodes


def preprocess_labels_only(item, task_list=None):
    task_list = ["y"] if task_list is None else task_list
    item["labels"] = {}
    for task in task_list:
        if task in item.keys():
            item["labels"][task] = item[task]

    return item


def preprocess_item(item, keep_features=True):
    dataset_name = item["dataset_name"][0]
    max_degree = item["max_degree"] if "max_degree" in item else None
    if max_degree is not None:
        max_degree = torch.max(torch.tensor(max_degree).flatten()).item()

    if keep_features and "edge_attr" in item.keys():
        edge_int_feature = np.asarray(item["edge_attr"], dtype=np.int32)
    else:
        edge_int_feature = np.ones((len(item["edge_index"][0]), 1), dtype=np.int32)  # same embedding for all

    if keep_features and "node_feat" in item.keys():
        node_int_feature = np.asarray(item["node_feat"], dtype=np.int32)
    else:
        node_int_feature = np.ones((item["num_nodes"], 1), dtype=np.int32)  # same embedding for all

    edge_index = np.asarray(item["edge_index"], dtype=np.int32)

    node_data = convert_to_single_emb_node(node_int_feature, dataset_name, max_degree)

    if len(edge_int_feature.shape) == 1:
        edge_int_feature = edge_int_feature[:, None]

    # We usually have either bond features as edge features or pre-defined one-hot features.
    # A small offset should be enough
    edge_data = convert_to_single_emb(edge_int_feature, offset=5)

    num_nodes = item["num_nodes"]
    dense_adj = np.zeros([num_nodes, num_nodes], dtype=bool)
    dense_adj[edge_index[0], edge_index[1]] = True

    in_degree = np.sum(dense_adj, axis=1).reshape(-1)
    lap_eigvec, lap_eigval = lap_eig(dense_adj, num_nodes, in_degree)  # [num_nodes, num_nodes], [num_nodes,]
    lap_eigval = np.broadcast_to(lap_eigval[None, :], lap_eigvec.shape)

    # +1 are to shift indexes, for nn.Embedding with pad_index=0
    item["node_data"] = node_data + 1
    item["edge_data"] = edge_data + 1
    item["edge_index"] = edge_index
    item["in_degree"] = in_degree + 1
    item["out_degree"] = in_degree + 1  # for undirected graph, directed graphs not managed atm
    item["lap_eigvec"] = lap_eigvec
    item["lap_eigval"] = lap_eigval
    if "labels" not in item:
        item["labels"] = item["y"]  # default label tends to be y

    return item


class TokenGTDataCollator:
    def __init__(self, spatial_pos_max=20):
        # self.tokenizer = tokenizer
        self.spatial_pos_max = spatial_pos_max

    @torch.no_grad()
    def __call__(self, features: List[dict]) -> Dict[str, Any]:
        # On the fly processing is done for very large batches which do not fit in storage
        if not isinstance(features[0], Mapping):
            features = [vars(f) for f in features]

        batch = {}

        batch["num_nodes"] = torch.tensor([i["num_nodes"] for i in features])
        batch["edge_num"] = torch.tensor([len(i["edge_data"]) for i in features])
        max_n = max(batch["num_nodes"])

        # TODO: check the accuracy of the comments on shape
        batch["edge_index"] = torch.cat(
            [torch.tensor(i["edge_index"], dtype=torch.long) for i in features], dim=1
        )  # [2, sum(edge_num)]
        batch["edge_data"] = torch.cat(
            [torch.tensor(i["edge_data"], dtype=torch.long) for i in features]
        )  # [sum(edge_num), edge embedding size],
        batch["node_data"] = torch.cat(
            [torch.tensor(i["node_data"], dtype=torch.long) for i in features]
        )  # [sum(node_num), node embedding size],
        batch["in_degree"] = torch.cat(
            [torch.tensor(i["in_degree"], dtype=torch.long) for i in features]
        )  # [sum(node_num),],
        batch["out_degree"] = torch.cat(
            [torch.tensor(i["out_degree"], dtype=torch.long) for i in features]
        )  # [sum(node_num),],
        batch["lap_eigvec"] = torch.cat(
            [
                F.pad(
                    torch.tensor(i["lap_eigvec"], dtype=torch.float),
                    (0, max_n - len(i["lap_eigvec"][0])),
                    value=float("0"),
                )
                for i in features
            ]
        )
        batch["lap_eigval"] = torch.cat(
            [
                F.pad(
                    torch.tensor(i["lap_eigval"], dtype=torch.float),
                    (0, max_n - len(i["lap_eigval"][0])),
                    value=float("0"),
                )
                for i in features
            ]
        )

        batch["labels"] = {}
        sample = torch.tensor(features[0]["labels"]).squeeze()

        if sample.ndim == 0:
            sample = sample.unsqueeze(0)

        if len(sample) == 1:  # one task
            if isinstance(sample[0].item(), float):  # regression
                batch["labels"] = torch.cat(
                    [torch.tensor(i["labels"], dtype=torch.float) for i in features]
                )  # [batch_size,]
            else:  # binary classification
                batch["labels"] = torch.cat(
                    [torch.tensor(i["labels"], dtype=torch.long) for i in features]
                )  # [batch_size,]
        else:  # multi task classification, left to float to keep the NaNs
            batch["labels"] = torch.stack(
                [torch.tensor(i["labels"], dtype=torch.float) for i in features], dim=0
            ).squeeze()  # [batch_size, num_classes, ]

        return batch
