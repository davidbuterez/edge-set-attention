import torch
import torch.nn as nn

# Taken from https://github.com/davidbuterez/st-no-gnn-general-refactor/blob/540011477c606cee4436bdba0a9c30cd9f4fe586/code/Exphormer/graphgps/encoder/

class LapPENodeEncoder(torch.nn.Module):
    """Laplace Positional Embedding node encoder.

    LapPE of size dim_pe will get appended to each node feature vector.
    If `expand_x` set True, original node features will be first linearly
    projected to (dim_emb - dim_pe) size and the concatenated with LapPE.

    Args:
        dim_emb: Size of final node embedding
        expand_x: Expand node features `x` from dim_in to (dim_emb - dim_pe)
    """

    def __init__(self):
        super().__init__()

        dim_pe = 4  # Size of Laplace PE embedding
        model_type = 'DeepSet' # Encoder NN model type for PEs
        if model_type not in ['Transformer', 'DeepSet']:
            raise ValueError(f"Unexpected PE model {model_type}")
        self.model_type = model_type
        n_layers = 2  # Num. layers in PE encoder model
        n_heads = 4 # Num. attention heads in Trf PE encoder
        post_n_layers = 0  # Num. layers to apply after pooling
        max_freqs = 1  # Num. eigenvectors (frequencies)
        norm_type = "none"  # Raw PE normalization layer type
        self.pass_as_var = True  # Pass PE also as a separate variable

        # Initial projection of eigenvalue and the node's eigenvector value
        self.linear_A = nn.Linear(2, dim_pe)
        if norm_type == 'batchnorm':
            self.raw_norm = nn.BatchNorm1d(max_freqs)
        else:
            self.raw_norm = None

        if model_type == 'Transformer':
            # Transformer model for LapPE
            encoder_layer = nn.TransformerEncoderLayer(d_model=dim_pe,
                                                       nhead=n_heads,
                                                       batch_first=True)
            self.pe_encoder = nn.TransformerEncoder(encoder_layer,
                                                    num_layers=n_layers)
        else:
            # DeepSet model for LapPE
            layers = []
            if n_layers == 1:
                layers.append(nn.ReLU())
            else:
                self.linear_A = nn.Linear(2, 2 * dim_pe)
                layers.append(nn.ReLU())
                for _ in range(n_layers - 2):
                    layers.append(nn.Linear(2 * dim_pe, 2 * dim_pe))
                    layers.append(nn.ReLU())
                layers.append(nn.Linear(2 * dim_pe, dim_pe))
                layers.append(nn.ReLU())
            self.pe_encoder = nn.Sequential(*layers)

        self.post_mlp = None
        if post_n_layers > 0:
            # MLP to apply post pooling
            layers = []
            if post_n_layers == 1:
                layers.append(nn.Linear(dim_pe, dim_pe))
                layers.append(nn.ReLU())
            else:
                layers.append(nn.Linear(dim_pe, 2 * dim_pe))
                layers.append(nn.ReLU())
                for _ in range(post_n_layers - 2):
                    layers.append(nn.Linear(2 * dim_pe, 2 * dim_pe))
                    layers.append(nn.ReLU())
                layers.append(nn.Linear(2 * dim_pe, dim_pe))
                layers.append(nn.ReLU())
            self.post_mlp = nn.Sequential(*layers)


    def forward(self, EigVals, EigVecs):
        # if not (hasattr(batch, 'EigVals') and hasattr(batch, 'EigVecs')):
        #     raise ValueError("Precomputed eigen values and vectors are "
        #                      f"required for {self.__class__.__name__}; "
        #                      "set config 'posenc_LapPE.enable' to True")
        # EigVals = batch.EigVals
        # EigVecs = batch.EigVecs

        if self.training:
            sign_flip = torch.rand(EigVecs.size(1), device=EigVecs.device)
            sign_flip[sign_flip >= 0.5] = 1.0
            sign_flip[sign_flip < 0.5] = -1.0
            EigVecs = EigVecs * sign_flip.unsqueeze(0)

        pos_enc = torch.cat((EigVecs.unsqueeze(2), EigVals), dim=2) # (Num nodes) x (Num Eigenvectors) x 2
        empty_mask = torch.isnan(pos_enc)  # (Num nodes) x (Num Eigenvectors) x 2

        pos_enc[empty_mask] = 0  # (Num nodes) x (Num Eigenvectors) x 2
        if self.raw_norm:
            pos_enc = self.raw_norm(pos_enc)
        pos_enc = self.linear_A(pos_enc)  # (Num nodes) x (Num Eigenvectors) x dim_pe

        # PE encoder: a Transformer or DeepSet model
        if self.model_type == 'Transformer':
            pos_enc = self.pe_encoder(src=pos_enc,
                                      src_key_padding_mask=empty_mask[:, :, 0])
        else:
            pos_enc = self.pe_encoder(pos_enc)

        # Remove masked sequences; must clone before overwriting masked elements
        pos_enc = pos_enc.clone().masked_fill_(empty_mask[:, :, 0].unsqueeze(2),
                                               0.)

        # Sum pooling
        pos_enc = torch.sum(pos_enc, 1, keepdim=False)  # (Num nodes) x dim_pe

        # MLP post pooling
        if self.post_mlp is not None:
            pos_enc = self.post_mlp(pos_enc)  # (Num nodes) x dim_pe

        return pos_enc

        # # Expand node features if needed
        # if self.expand_x:
        #     h = self.linear_x(batch.x)
        # else:
        #     h = batch.x
        # # Concatenate final PEs to input embedding
        # batch.x = torch.cat((h, pos_enc), 1)
        # # Keep PE also separate in a variable (e.g. for skip connections to input)
        # if self.pass_as_var:
        #     batch.pe_LapPE = pos_enc
        # return batch