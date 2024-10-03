
import torch
import torch.nn as nn
import numpy as np
import ray
from .utils import split_list, shifted_pairs, product, combinations
from .writhe_nn import ndot, ncross, nnorm, writhe_segments


# how to compute writhe angle (omega) using ortho projection rather than cross products. Still appears to be slower than cross products.
# smat = xyz[:, segments]
# n_segments = smat.shape[1]
# indices = torch.LongTensor([[0, 1, 0, 2],
#                             [3, 2, 3, 1],
#                             [1, 3, 2, 0]
#                            ]).T
#
# dx = nnorm((-smat[:, :, :2, None, :] + smat[:, :, None, 2:, :]).reshape(-1, n_segments, 4, 3)) #displacements
# omega = (-torch.arcsin(uproj(dx[:, :, indices[:,:-1]], dx[:, :, indices[:, -1], None]).prod(dim=-2).sum(-1).clip(-1, 1))).sum(-1)

def get_segments(n: int = None,
                 length: int = 1,
                 index0: np.ndarray = None,
                 index1: np.ndarray = None,
                 tensor: bool = False):
    """
    Function to retrieve indices of segment pairs for various use cases.
    Returns an (n_segment_pairs, 4) array where each row (quadruplet) contains : (start1, end1, start2, end2)
    """

    if all(i is None for i in (index0, index1)):
        assert n is not None, \
            "Must provide indices (index0:array, (optionally) index1:array) or the number of points (n: int)"
        segments = combinations(shifted_pairs(np.arange(n), length)).reshape(-1, 4)
        segments = segments[~(segments[:, 1] == segments[:, 2])]
        return torch.from_numpy(segments).long() if tensor else segments

    else:
        assert index0 is not None, ("If providing only one set of indices, must set the index0 argument \n"
                                    "Cannot only supply the index1 argument (doesn't make sense in this context")
        if index1 is not None:
            segments = product(*[shifted_pairs(i, length) for i in (index0, index1)]).reshape(-1, 4)
            return torch.from_numpy(segments).long() if tensor else segments
        else:
            segments = combinations(shifted_pairs(index0, length)).reshape(-1, 4)
            segments = segments[~(segments[:, 1] == segments[:, 2])]
            return torch.from_numpy(segments).long() if tensor else segments


class TorchWrithe(nn.Module):
    """
    Class to compute writhe using torch
    """

    def __init__(self,
                 n_atoms: int,
                 segment_length: int = 1,
                 ):
        super().__init__()

        self.register_buffer("n_atoms_", torch.LongTensor([n_atoms]))
        self.segment_length = segment_length

    @property
    def n_atoms(self):
        return self.n_atoms_.item()

    @property
    def segment_length(self):
        return self.segment_length_

    @segment_length.setter
    def segment_length(self, length: int):
        self.segment_length_ = length
        self.register_buffer("segments", get_segments(self.n_atoms, length, tensor=True))
        return

    def forward(self, xyz: torch.Tensor):
        return writhe_segments(self.segments, xyz)


# for computing but not deep learning, attempts to avoid leaving tensors hiding on GPUs
def writhe_smat(smat: torch.Tensor, device: int = 0):
    """
    Function to use GPU in computation of the writhe; NOT to be used in neural nets
    smat: array of shape(Nframes, Nsegments, 4, 3), coordinate array sliced with segments array

    """
    smat = smat.to(device)

    displacements = nnorm((-smat[:, :, :2, None, :] + smat[:, :, None, 2:, :]
                           ).reshape(-1, smat.shape[1], 4, 3))

    signs = torch.sign(ndot(ncross(smat[:, :, 3] - smat[:, :, 2],
                                   smat[:, :, 1] - smat[:, :, 0]),
                            displacements[:, :, 0]))

    del smat

    crosses = nnorm(ncross(displacements[:, :, [0, 1, 3, 2]], displacements[:, :, [1, 3, 2, 0]]))

    del displacements

    omega = torch.arcsin(ndot(crosses[:, :, [0, 1, 2, 3]],
                              crosses[:, :, [1, 2, 3, 0]]).clip(-1, 1)).sum(2)

    del crosses

    return torch.squeeze((omega * signs) / (2 * torch.pi)).cpu()


def peak_mem_writhe(n_segments, n_samples):
    """
    Return the estimated peak memory required by a broadcasted writhe calculation using
    n_segments and n_samples in GB.

    This was determined through numerical anaylsis, however, can only be considered a rule of thumb.

    Torch seems to need about 2 GB more than expected on some machines.
    """
    return (n_segments * 2.01708461e-07 + 5.93515514e-08) * n_samples


def get_available_memory(device: int = 0):
    """return VRAM available on a device in GB"""

    assert torch.cuda.is_available(), "CUDA is not available"

    return (torch.cuda.get_device_properties(device).total_memory
            - torch.cuda.memory_allocated(device)) / 1024 ** 3


def get_segment_batch_size(n_samples, device: int = 0):
    mem = get_available_memory() - 2  # torch seems to need 2 GB more than expected

    return int(((mem / n_samples) - 5.93515514e-08) / 2.01708461e-07)


def writhe_segments_cuda(segments, xyz, device: int = 0):
    segments = segments.unsqueeze(0) if segments.ndim < 2 else segments
    smat = (xyz.unsqueeze(0) if xyz.ndim < 3 else xyz)[:, segments]
    result = writhe_smat(smat=smat, device=device)
    torch.cuda.empty_cache()
    return result


def writhe_segments_minibatches(segment_chunks: list, xyz: torch.tensor, device: int):
    xyz = ray.get(xyz) if not isinstance(xyz, torch.Tensor) else xyz
    return torch.cat([writhe_segments_cuda(segments=i, xyz=xyz, device=device) for i in segment_chunks], -1)


def calc_writhe_parallel_cuda(segments: torch.Tensor,
                              xyz: torch.Tensor,
                              reduce_batch_size: int = 0):
    batch_size = get_segment_batch_size(len(xyz)) - reduce_batch_size

    if batch_size > len(segments):
        return writhe_segments_cuda(segments, xyz).numpy()

    chunks = list(torch.split(segments, batch_size))

    if len(segments) < 5 * batch_size or torch.cuda.device_count() == 1:
        return writhe_segments_minibatches(chunks, xyz, 0).numpy()

    else:
        minibatches = split_list(chunks, torch.cuda.device_count())
        xyz_ref = ray.put(xyz)
        fxn = ray.remote(num_gpus=torch.cuda.device_count())(writhe_segments_minibatches)
        return torch.cat(ray.get([fxn.remote(segment_chunks=j, xyz=xyz_ref, device=i)
                                  for i, j in enumerate(minibatches)]), -1).numpy()

#####################################    #####################################    #####################################     #####################################

# def apply_attention(self, Q: callable, K: callable, V: callable, A: callable,
#                     features: torch.Tensor, edges: torch.Tensor):
#
#     src_node, dst_node = (i.flatten() for i in self.edges)
#
#     attention_input = torch.cat([i(j) for i, j in
#                                  zip([Q, K, V],
#                                      [features[dst_node], features[src_node], edges])
#                                  ], dim=-1)
#
#     logits = torch.exp(A(attention_input).flatten())
#
#     attention = (logits / scatter(logits, dst_node)[dst_node]).unsqueeze(-1)
#
#     return scatter(edges * attention, dst_node, dim=0)

# def get_twist_edges(n: int):
#     segments = np.concatenate([shifted_pairs(np.arange(1, n - 1), 1), shifted_pairs(np.arange(2, n), 1)])
#     return torch.LongTensor(segments)
#
# @torch.jit.script
# def compute_twist(xyz: torch.Tensor):
#     s = nnorm(xyz[:, 1:] - xyz[:, :-1])
#     p = nnorm(ncross(s[:, 1:], s[:, :-1]))
#     return torch.arccos(ndot(p[:, :1], p[:, 1:])) * torch.sign(ndot(p[:, :1], s[:, 1:-1])) / torch.pi
#
# class MLP(torch.nn.Module):
#     def __init__(self, f_in, f_hidden, f_out, skip_connection=False):
#         super().__init__()
#         self.skip_connection = skip_connection
#
#         self.mlp = torch.nn.Sequential(
#             torch.nn.Linear(f_in, f_hidden),
#             torch.nn.LayerNorm(f_hidden),
#             torch.nn.SiLU(),
#             torch.nn.Linear(f_hidden, f_hidden),
#             torch.nn.LayerNorm(f_hidden),
#             torch.nn.SiLU(),
#             torch.nn.Linear(f_hidden, f_out),
#         )
#
#     def forward(self, x):
#         if self.skip_connection:
#             return x + self.mlp(x)
#
#         return self.mlp(x)
#
#
# class TwistMessage(nn.Module):
#     def __init__(self,
#                  n_atoms: int,
#                  n_features: int,
#                  batch_size: int,
#                  bins: int = 100,
#                  bin_start: float = -1,
#                  bin_end: float = 1,
#                  ):
#         super().__init__()
#
#         self.soft_one_hot = partial(soft_one_hot_linspace,
#                                     start=bin_start,
#                                     end=bin_end,
#                                     number=bins,
#                                     basis="gaussian",
#                                     cutoff=False)
#
#         std = 1. / math.sqrt(n_features)
#
#         self.register_parameter("basis",
#                                 torch.nn.Parameter(torch.Tensor(1, 1, bins, n_features).normal_(-std, std),
#                                                    requires_grad=True)
#                                 )
#
#         edges = get_twist_edges(n_atoms).T
#         self.register_buffer("edges", torch.cat([i * n_atoms + edges for i in range(batch_size)], axis=1).long())
#
#         self.query = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#         self.key = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#         self.value = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#         self.attention = nn.Sequential(nn.Linear(int(3 * n_features), 1), nn.LeakyReLU())
#
#
#
#         #self.mlp = MLP(n_features, n_features, n_features)
#
#         self.n_features = n_features
#
#         self.register_buffer("n_atoms_", torch.LongTensor([n_atoms]))
#
#         self.register_buffer("batch_size_", torch.LongTensor([batch_size]))
#
#     @property
#     def n_atoms(self):
#         return self.n_atoms_.item()
#
#     def forward(self, batch):
#         src_node, dst_node = self.edges
#         features = batch.invariant_node_features.clone()
#
#         x = batch.x.clone().reshape(-1, self.n_atoms, 3)
#
#         twist = (self.soft_one_hot(compute_twist(x)).unsqueeze(-1) * self.basis).sum(-2).reshape(-1, self.n_features).repeat(2, 1)
#
#         attention_input = torch.cat([getattr(self, i)(j) for i, j in
#                                      zip(["query", "key", "value"], [features[dst_node], features[src_node], twist])
#                                      ], dim=-1)
#
#         weights = torch.exp(self.attention(attention_input).flatten())
#
#         attention = (weights / scatter(weights, dst_node)[dst_node]).unsqueeze(-1)
#
#         message = scatter(twist * attention, dst_node, dim=0)
#
#         batch.invariant_node_features = features + message
#
#         return batch
#
#
# class KnotMessage(nn.Module):
#     """
#     Twist and Writhe message layer.
#
#     """
#
#     def __init__(self,
#                  n_atoms: int,
#                  n_features: int,
#                  batch_size: int,
#                  bins: int = 300,
#                  node_feature: str = "invariant_node_features",
#                  segment_length: int = 1,
#                  bin_start: float = -0.3,
#                  bin_end: float = 0.3,
#                  residual: bool = True
#                  ):
#
#         super().__init__()
#
#         # prerequisit information
#         # writhe edges
#         segments = get_segments(n_atoms, length=segment_length, tensor=True)
#         writhe_edges = torch.cat([segments[:, [0, 2]], segments[:, [1, 3]]]).T
#
#         # twist edges
#         twist_edges = get_twist_edges(n_atoms).T
#         edges = torch.cat([writhe_edges, twist_edges], dim=-1)
#
#
#         self.register_buffer("edges", torch.cat([i * n_atoms + edges for i in range(batch_size)], axis=1).long())
#         self.register_buffer("segments", segments)
#         self.register_buffer("n_atoms_", torch.LongTensor([n_atoms]))
#         self.register_buffer("batch_size_", torch.LongTensor([batch_size]))
#         #self.register_buffer("segment_length", torch.LongTensor([segment_length]))
#
#         self.node_feature = node_feature
#         self.n_features = n_features
#         self.residual = residual
#         self.bin_start = bin_start
#         self.bin_end = bin_end
#
#         # writhe embedding
#
#         self.soft_one_hot_writhe = partial(soft_one_hot_linspace,
#                                     start=self.bin_start,
#                                     end=self.bin_end,
#                                     number=bins,
#                                     basis="gaussian",
#                                     cutoff=False)
#
#         self.soft_one_hot_twist = partial(soft_one_hot_linspace,
#                                           start=self.bin_start,
#                                           end=self.bin_end,
#                                           number=bins,
#                                           basis="gaussian",
#                                           cutoff=False)
#
#         std = 1. / math.sqrt(n_features)
#
#         self.register_parameter("writhe_basis",
#                                 torch.nn.Parameter(torch.Tensor(1, 1, bins, n_features).uniform_(-std, std),
#                                                    # normal_(0, std),
#                                                    requires_grad=True)
#                                 )
#
#
#         self.register_parameter("twist_basis",
#                                 torch.nn.Parameter(torch.Tensor(1, 1, bins, n_features).uniform_(-std, std),
#                                                    # normal_(0, std),
#                                                    requires_grad=True)
#                                 )
#
#         # attention mechanism
#
#         self.query = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#         self.key = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#         self.value = nn.Sequential(nn.Linear(n_features, n_features), nn.LeakyReLU())
#
#
#         # self.query = nn.Sequential(nn.Linear(n_features, n_features), nn.SiLU())
#         # self.key = nn.Sequential(nn.Linear(n_features, n_features), nn.SiLU())
#         # self.value = nn.Sequential(nn.Linear(n_features, n_features), nn.Tanh())
#
#         # self.query = nn.Linear(n_features, n_features)
#         # self.key = nn.Linear(n_features, n_features)
#         # self.value = nn.Linear(n_features, n_features)
#
#         self.attention = nn.Sequential(nn.Linear(int(3 * n_features), 1), nn.LeakyReLU())
#
#     @property
#     def n_atoms(self):
#         return self.n_atoms_.item()
#
#     def embed_writhe(self, wr):
#         return (self.soft_one_hot_writhe(wr).unsqueeze(-1) * self.writhe_basis).sum(-2)
#
#     def embed_twist(self, tw):
#         return (self.soft_one_hot_twist(tw).unsqueeze(-1) * self.twist_basis).sum(-2)
#
#     def compute_writhe(self, x):
#         return self.embed_writhe(
#                writhe_segments(self.segments, x.x.reshape(-1, self.n_atoms, 3))
#                ).repeat(1, 2, 1).reshape(-1, self.n_features)
#
#     def compute_twist(self, x):
#         return self.embed_twist(
#                compute_twist(x.x.reshape(-1, self.n_atoms, 3))
#                ).repeat(1, 2, 1).reshape(-1, self.n_features)
#
#     def forward(self, x, update=True):
#
#         features = getattr(x, self.node_feature).clone()
#
#         src_node, dst_node = (i.flatten() for i in self.edges)
#
#         writhe = self.compute_writhe(x)
#
#         twist = self.compute_twist(x)
#
#         dscrs = torch.cat([writhe, twist], dim=0)
#
#         attention_input = torch.cat([getattr(self, i)(j) for i, j in
#                                      zip(["query", "key", "value"], [features[dst_node], features[src_node], dscrs])
#                                      ], dim=-1)
#
#         weights = torch.exp(self.attention(attention_input).flatten())
#
#         attention = (weights / scatter(weights, dst_node)[dst_node]).unsqueeze(-1)
#
#         message = scatter(dscrs * attention, dst_node, dim=0)
#
#         if update:
#
#             x[self.node_feature] = features + message if self.residual else message
#
#             return x
#
#         else:
#             return features + message if self.residual else message

