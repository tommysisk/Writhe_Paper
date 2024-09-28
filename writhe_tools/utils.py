#!/usr/bin/env python

import numpy as np
import torch
import pickle
import itertools
import time
from numpy_indexed import group_by as group_by_
import functools
import gc


def split_list(lst, n):
    # Determine the size of each chunk
    k, m = divmod(len(lst), n)  # k is the size of each chunk, m is the remainder

    # If the length of the list is less than n, adjust n
    if len(lst) < n:
        n = len(lst)

    # Split the list into n sublists, distributing the remainder elements equally
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


def sort_by_val_in(indices: np.ndarray,
                   value: np.ndarray,
                   max: bool = True):
    stride = -1 if max else 1
    return indices[np.argsort(value[indices])[::stride]]


def sort_indices_list(indices_list: list,
                      obs: "numpy array with values corresponding to indices",
                      max: bool = True):
    """Sort each array in a list of indices arrays based on their values in obs."""
    sort = functools.partial(sort_by_val_in, value=obs, max=max)
    return list(map(sort, indices_list))


def group_by(keys: np.ndarray,
             values: np.ndarray = None,
             reduction: callable = None):

    if reduction is not None:
        values = np.ones_like(keys) / len(keys) if values is None else values

        if values.squeeze().ndim > 1:

            return np.stack([i[-1] for i in group_by_(keys=keys, values=values, reduction=reduction)])

        else:
            return np.asarray(group_by_(keys=keys, values=values, reduction=reduction))[:, -1]

    values = np.arange(len(keys)) if values is None else values

    return group_by_(keys).split_array_as_list(values)




def product(x: np.ndarray, y: np.ndarray):
    return np.asarray(list(itertools.product(x, y)))


def combinations(x):
    return np.asarray(list(itertools.combinations(x, 2)))


def shifted_pairs(x: np.ndarray, shift: int, ax: int = 1):
    return np.stack([x[:-shift], x[shift:]], ax)


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


def to_numpy(x: "int, list or array"):
    if isinstance(x, np.ndarray):
        return x
    if isinstance(x, (int, float, np.int64, np.int32, np.float32, np.float64)):
        return np.array([x])
    if isinstance(x, list):
        return np.asarray(x)
    if isinstance(x, (map, filter, tuple)):
        return np.asarray(list(x))


def load_dict(file):
    with open(file, "rb") as handle:
        dic_loaded = pickle.load(handle)
    return dic_loaded


def save_dict(file, dict):
    with open(file, "wb") as handle:
        pickle.dump(dict, handle)
    return None


class Timer:
    """import time"""

    def __init__(self, check_interval: "the time (hrs) after the call method should return false" = 1):

        self.start_time = time.time()
        self.interval = check_interval * (60 ** 2)

    def __call__(self):
        if abs(time.time() - self.start_time) > self.interval:
            self.start_time = time.time()
            return True
        else:
            return False

    def time_remaining(self):
        sec = max(0, self.interval - abs(time.time() - self.start_time))
        hrs = sec // (60 ** 2)
        mins_remaining = (sec / 60 - hrs * (60))
        mins = mins_remaining // 1
        secs = (mins_remaining - mins) * 60
        hrs, mins, secs = [int(i) for i in [hrs, mins, secs]]
        print(f"{hrs}:{mins}:{secs}")
        return None

    # for context management
    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        self.end = time.time()
        self.interval = self.end - self.start
        print(f"Time elapsed : {self.interval} s")
        return self.interval

def cleanup():
    gc.collect()  # Clean up unreferenced memory
    with torch.no_grad():
        torch.cuda.empty_cache()


def window_average(x, N):
    cumsum = np.cumsum(np.insert(x, 0, 0))
    return (cumsum[N:] - cumsum[:-N]) / float(N)




