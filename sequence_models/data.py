from typing import List, Any, Iterable
import random
import math
import subprocess
import string
import json
from os import path

import numpy as np
import torch
from torch.utils.data import Dataset, Sampler, BatchSampler
import pandas as pd

from sequence_models.utils import Tokenizer
from sequence_models.constants import PAD, START, STOP, MASK
from sequence_models.constants import ALL_AAS, trR_ALPHABET
from sequence_models.gnn import get_node_features, get_edge_features, get_mask, get_k_neighbors, replace_nan


class UniRefDataset(Dataset):
    """
    Dataset that pulls from UniRef/Uniclust downloads.

    The data folder should contain the following:
    - 'consensus.fasta': consensus sequences, no line breaks in sequences
    - 'splits.json': a dict with keys 'train', 'valid', and 'test' mapping to lists of indices
    - 'lengths_and_offsets.npz': byte offsets for the 'consensus.fasta' and sequence lengths
    """

    def __init__(self, data_dir: str, split: str, structure: bool=False):
        self.data_dir = data_dir
        self.split = split
        self.structure = structure
        with open(data_dir + 'splits.json', 'r') as f:
            self.indices = json.load(f)[self.split]
        metadata = np.load(self.data_dir + 'lengths_and_offsets.npz')
        self.offsets = metadata['seq_offsets']

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, idx):
        idx = self.indices[idx]
        offset = self.offsets[idx]
        with open(self.data_dir + 'consensus.fasta') as f:
            f.seek(offset)
            consensus = f.readline()[:-1]
        if self.structure:
            fname = self.data_dir + 'structures/%08d.npz' %idx
            if path.isfile(fname):
                structure = np.load(fname)
                return consensus, structure
            else:
                return consensus, None
        return (consensus, )


class FFDataset(Dataset):

    def __init__(self, stem, max_len=np.inf, tr_only=True):
        self.index = stem + 'ffindex'
        self.data = stem + 'ffdata'
        result = subprocess.run(['wc', '-l', self.index], stdout=subprocess.PIPE)
        self.length = int(result.stdout.decode('utf-8').split(' ')[0])
        self.tokenizer = Tokenizer(trR_ALPHABET)
        self.table = str.maketrans(dict.fromkeys(string.ascii_lowercase))
        self.max_len = max_len
        self.tr_only = tr_only

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        result = subprocess.run(['ffindex_get', self.data, self.index, '-n', str(idx + 1)],
                                stdout=subprocess.PIPE)
        a3m = result.stdout.decode('utf-8')
        seqs = []
        for line in a3m.split('\n'):
            # skip labels
            if len(line) == 0:
                continue
            if line[0] == '#':
                continue
            if line[0] != '>':
                # remove lowercase letters and right whitespaces
                s = line.rstrip().translate(self.table)
                if self.tr_only:
                    s = ''.join([a if a in trR_ALPHABET else '-' for a in s])
                if len(s) > self.max_len:
                    return torch.tensor([])
                seqs.append(s)
        seqs = torch.tensor([self.tokenizer.tokenize(s) for s in seqs])
        return seqs


class FlatDataset(Dataset):

    def __init__(self, fpath, offsets, cols=[1]):
        self.fpath = fpath
        self.offsets = offsets
        self.cols = cols

    def __len__(self):
        return len(self.offsets)

    def __getitem__(self, idx):
        with open(self.fpath, 'r') as f:
            f.seek(self.offsets[idx])
            line = f.readline()[:-1]  # strip the \n
            line = line.split(',')
            return [line[i] for i in self.cols]


class CSVDataset(Dataset):

    def __init__(self, fpath=None, df=None, split=None, outputs=[]):
        if df is None:
            self.data = pd.read_csv(fpath)
        else:
            self.data = df
        if split is not None:
            self.data = self.data[self.data['split'] == split]
        self.outputs = outputs
        self.data = self.data[['sequence'] + self.outputs]

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        row = self.data.loc[idx]
        return [row['sequence'], *row[self.outputs]]


class SimpleCollater(object):

    def __init__(self, alphabet: str, pad=False):
        self.pad = pad
        self.tokenizer = Tokenizer(alphabet)

    def __call__(self, batch: List[Any], ) -> List[torch.Tensor]:
        data = tuple(zip(*batch))
        sequences = data[0]
        prepped = self._prep(sequences)
        return prepped

    def _prep(self, sequences):
        sequences = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in sequences]
        if self.pad:
            pad_idx = self.tokenizer.alphabet.index(PAD)
            sequences = _pad(sequences, pad_idx)
        else:
            sequences = torch.stack(sequences)
        return (sequences, )


class LMCollater(SimpleCollater):

    def __init__(self, alphabet: str, pad=False, backwards=False):
        super().__init__(alphabet, pad=pad)
        self.backwards = backwards

    def _prep(self, sequences):
        return self._tokenize_and_mask(*self._split(sequences))

    def _split(self, sequences):
        if not self.backwards:
            src = [START + s for s in sequences]
            tgt = [s + STOP for s in sequences]
        else:
            src = [STOP + s[::-1] for s in sequences]
            tgt = [s[::-1] + START for s in sequences]
        return src, tgt

    def _tokenize_and_mask(self, src, tgt):
        src = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in src]
        tgt = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in tgt]
        mask = [torch.ones_like(t) for t in tgt]
        pad_idx = self.tokenizer.alphabet.index(PAD)
        src = _pad(src, pad_idx)
        tgt = _pad(tgt, pad_idx)
        mask = _pad(mask, 0)
        return src, tgt, mask


def _pad(tokenized: List[torch.Tensor], value: int) -> torch.Tensor:
    batch_size = len(tokenized)
    max_len = max(len(t) for t in tokenized)
    output = torch.zeros((batch_size, max_len), dtype=tokenized[0].dtype) + value
    for row, t in enumerate(tokenized):
        output[row, :len(t)] = t
    return output


class AncestorCollater(LMCollater):

    def __call__(self, batch):
        data = tuple(zip(*batch))
        sequences, ancestors = data[:2]
        prepped = self._prep(sequences, ancestors)
        return prepped

    def _prep(self, sequences, ancestors):
        if self.backwards:
            sequences = [s[::-1] for s in sequences]
            ancestors = [a[::-1] for a in ancestors]
        src = [START + s + STOP + a for s, a in zip(sequences, ancestors)]
        tgt = [s + STOP + a + STOP for s, a in zip(sequences, ancestors)]
        return self._tokenize_and_mask(src, tgt)


class MLMCollater(SimpleCollater):

    def _prep(self, sequences):
        tgt = list(sequences[:])
        src = []
        mask = []
        for seq in sequences:
            if len(seq) == 0:
                tgt.remove(seq)
                continue
            mod_idx = random.sample(list(range(len(seq))), int(len(seq) * 0.15))
            if len(mod_idx) == 0:
                mod_idx = [np.random.choice(len(seq))]  # make sure at least one aa is chosen
            seq_mod = list(seq)
            for idx in mod_idx:
                p = np.random.uniform()
                if p <= 0.10:  # do nothing
                    mod = seq[idx]
                elif 0.10 < p <= 0.20:  # replace with random amino acid
                    mod = np.random.choice([i for i in ALL_AAS if i != seq[idx]])
                else:  # mask
                    mod = MASK
                seq_mod[idx] = mod
            src.append(''.join(seq_mod))
            m = torch.zeros(len(seq_mod))
            m[mod_idx] = 1
            mask.append(m)
        src = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in src]
        tgt = [torch.LongTensor(self.tokenizer.tokenize(s)) for s in tgt]
        pad_idx = self.tokenizer.alphabet.index(PAD)
        src = _pad(src, pad_idx)
        tgt = _pad(tgt, pad_idx)
        mask = _pad(mask, 0)
        return src, tgt, mask


class StructureCollater(object):

    def __init__(self, sequence_collater: SimpleCollater, p_drop_structure=0.1, n_connections=20):
        self.sequence_collater = sequence_collater
        self.p_drop = p_drop_structure
        self.n_connections = n_connections

    def __call__(self, batch: List[Any], ) -> Iterable[torch.Tensor]:
        sequences, structures = tuple(zip(*batch))
        collated_seqs = self.sequence_collater._prep(sequences)
        ells = [len(s) for s in sequences]
        max_ell = max(ells) + 1
        n = len(sequences)
        nodes = torch.zeros(n, max_ell, 10)
        edges = torch.zeros(n, max_ell, self.n_connections, 6)
        connections = torch.zeros(n, max_ell, self.n_connections, dtype=torch.long)
        edge_mask = torch.zeros(n, max_ell, self.n_connections, 1)
        for i, (ell, structure) in enumerate(zip(ells, structures)):
            if structure is None:
                continue
            if np.random.random() < self.p_drop:
                continue
            # load features
            ## TODO: Check the ordering
            dist = torch.from_numpy(structure['0']).float()
            omega = torch.from_numpy(structure['1']).float()
            theta = torch.from_numpy(structure['2']).float()
            phi = torch.from_numpy(structure['3']).float()
            # process features
            V = get_node_features(omega, theta, phi)
            E_idx = get_k_neighbors(dist, self.n_connections)
            E = get_edge_features(dist, omega, theta, phi, E_idx)
            str_mask = get_mask(E)
            E = replace_nan(E)
            # reshape
            nc = min(ell, self.n_connections)
            nodes[i, 1: ell + 1] = V
            edges[i, 1: ell + 1, :nc] = E
            connections[i, 1: ell + 1, :nc] = E_idx
            str_mask = str_mask.view(1, ell, -1)
            edge_mask[i, 1: ell + 1, :nc, 0] = str_mask
        return (*collated_seqs, nodes, edges, connections, edge_mask)


class SortishSampler(Sampler):
    """Returns indices such that inputs with similar lengths are close together."""

    def __init__(self, sequence_lengths: Iterable, bucket_size: int, num_replicas: int = 1, rank: int = 0):
        self.data = np.argsort(sequence_lengths)
        self.num_replicas = num_replicas
        self.num_samples = int(math.ceil(len(self.data) * 1.0 / self.num_replicas))
        self.bucket_size = bucket_size
        n_buckets = int(np.ceil(len(self.data) / self.bucket_size))
        self.data = [self.data[i * bucket_size: i * bucket_size + bucket_size] for i in range(n_buckets)]
        self.rank = rank
        self.epoch = 0
        self.total_size = self.num_samples * self.num_replicas

    def __iter__(self):
        np.random.seed(self.epoch)
        for bucket in self.data:
            np.random.shuffle(bucket)
        np.random.shuffle(self.data)
        indices = [item for sublist in self.data for item in sublist]
        indices += indices[:(self.total_size - len(indices))]
        assert len(indices) == self.total_size
        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.num_samples
        return iter(indices)

    def __len__(self):
        return self.num_samples

    def set_epoch(self, epoch):
        self.epoch = epoch


class ApproxBatchSampler(BatchSampler):
    """
	Parameters:
	-----------
	sampler : Pytorch Sampler
		Choose base sampler class to use for bucketing

	max_tokens : int
		Maximum number of tokens per batch

	max_batch: int
		Maximum batch size

	sample_lengths : array-like
		List of lengths of sequences in the order of the dataset
	"""

    def __init__(self, sampler, max_tokens, max_batch, sample_lengths):
        self.longest_token = 0
        self.max_tokens = max_tokens
        self.max_batch = max_batch
        self.sampler = sampler
        self.sample_lengths = sample_lengths

    def __iter__(self):
        batch = []
        length = 0
        for idx in self.sampler:
            this_length = self.sample_lengths[idx]
            if (len(batch) + 1) * max(length, this_length) <= self.max_tokens:
                batch.append(idx)
                length = max(length, this_length)
                if len(batch) == self.max_batch:
                    yield batch
                    batch = []
                    length = 0
            else:
                yield batch
                batch = [idx]
                length = this_length
