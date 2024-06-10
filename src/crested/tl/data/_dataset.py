"""Dataset class for combining genome files and AnnData objects."""

from __future__ import annotations

from os import PathLike

import numpy as np
from anndata import AnnData
from loguru import logger
from pysam import FastaFile
from scipy.sparse import spmatrix
from tqdm import tqdm


class SequenceLoader:
    def __init__(
        self,
        genome_file: PathLike,
        in_memory: bool,
        always_reverse_complement: bool,
        max_stochastic_shift: int,
        regions: list[str] = None,
    ):
        self.genome = FastaFile(genome_file)
        self.in_memory = in_memory
        self.always_reverse_complement = always_reverse_complement
        self.max_stochastic_shift = max_stochastic_shift
        self.sequences = {}
        self.complement = str.maketrans("ACGT", "TGCA")
        self.regions = regions
        if self.in_memory:
            self._load_sequences_into_memory(self.regions)

    def _load_sequences_into_memory(self, regions: list[str]):
        """Load all sequences into memory (dict)."""
        logger.info("Loading sequences into memory...")
        for region in tqdm(regions):
            extended_sequence = self._get_extended_sequence(region)
            self.sequences[f"{region}:+"] = extended_sequence
            if self.always_reverse_complement:
                self.sequences[f"{region}:-"] = self._reverse_complement(
                    extended_sequence
                )

    def _get_extended_sequence(self, region: str) -> str:
        """Get sequence from genome file, extended for stochastic shifting."""
        chrom, start_end = region.split(":")
        start, end = map(int, start_end.split("-"))
        extended_start = max(0, start - self.max_stochastic_shift)
        extended_end = end + self.max_stochastic_shift
        return self.genome.fetch(chrom, extended_start, extended_end)

    def _reverse_complement(self, sequence: str) -> str:
        """Reverse complement a sequence."""
        return sequence.translate(self.complement)[::-1]

    def get_sequence(self, region: str, strand: str = "+", shift: int = 0) -> str:
        """Get sequence for a region, strand, and shift from memory or fasta."""
        key = f"{region}:{strand}"
        if self.in_memory:
            sequence = self.sequences[key]
        else:
            sequence = self._get_extended_sequence(region)

        chrom, start_end = region.split(":")
        start, end = map(int, start_end.split("-"))
        start_idx = self.max_stochastic_shift + shift
        end_idx = start_idx + (end - start)
        sub_sequence = sequence[start_idx:end_idx]

        # handle reverse complement on the go if not loaded into memory
        if (strand == "-") and (not self.in_memory):
            sub_sequence = self._reverse_complement(sub_sequence)

        return sub_sequence


class IndexManager:
    def __init__(self, indices: list[str], always_reverse_complement: bool):
        self.indices = indices
        self.always_reverse_complement = always_reverse_complement
        self.augmented_indices, self.augmented_indices_map = self._augment_indices(
            indices
        )

    def _augment_indices(self, indices: list[str]) -> tuple[list[str], dict[str, str]]:
        """Augment indices with strand information. Necessary if always reverse complement to map sequences back to targets."""
        augmented_indices = []
        augmented_indices_map = {}
        for region in indices:
            augmented_indices.append(f"{region}:+")
            augmented_indices_map[f"{region}:+"] = region
            if self.always_reverse_complement:
                augmented_indices.append(f"{region}:-")
                augmented_indices_map[f"{region}:-"] = region
        return augmented_indices, augmented_indices_map

    def shuffle_indices(self):
        """Shuffling of indices. Managed by subclass AnnDataLoader."""
        np.random.shuffle(self.indices)
        self.augmented_indices, self.augmented_indices_map = self._augment_indices(
            self.indices
        )


class AnnDataset:
    def __init__(
        self,
        anndata: AnnData,
        genome_file: PathLike,
        split: str = None,
        chromsizes_file: PathLike | None = None,
        in_memory: bool = True,
        random_reverse_complement: bool = False,
        always_reverse_complement: bool = False,
        max_stochastic_shift: int = 0,
    ):
        self._validate_init_args(random_reverse_complement, always_reverse_complement)
        self.anndata = self._split_anndata(anndata, split)
        self.split = split
        self.indices = list(self.anndata.var_names)
        self.in_memory = in_memory
        self.compressed = isinstance(self.anndata.X, spmatrix)
        self.chromsizes = chromsizes_file
        self.index_map = {index: i for i, index in enumerate(self.indices)}
        self.num_outputs = self.anndata.X.shape[0]
        self.random_reverse_complement = random_reverse_complement
        self.max_stochastic_shift = max_stochastic_shift
        self.shuffle = False  # managed by subclass AnnDataLoader

        if (chromsizes_file is None) and (max_stochastic_shift > 0):
            self._warn_no_chromsizes_file()

        self.sequence_loader = SequenceLoader(
            genome_file,
            in_memory,
            always_reverse_complement,
            max_stochastic_shift,
            self.indices,
        )
        self.index_manager = IndexManager(self.indices, always_reverse_complement)

    @staticmethod
    def _validate_init_args(
        random_reverse_complement: bool, always_reverse_complement: bool
    ):
        if random_reverse_complement and always_reverse_complement:
            raise ValueError(
                "Only one of `random_reverse_complement` and `always_reverse_complement` can be True."
            )

    @staticmethod
    def _warn_no_chromsizes_file():
        logger.warning(
            "Chromsizes file not provided when shifting. Will not check if shifted regions are within chromosomes",
        )

    @staticmethod
    def _split_anndata(anndata: AnnData, split: str) -> AnnData:
        """Return subset of anndata based on a given split column."""
        if "split" not in anndata.var.columns:
            raise KeyError(
                "No split column found in anndata.var. Run `pp.train_val_test_split` first."
            )
        subset = (
            anndata[:, anndata.var["split"] == split].copy()
            if split
            else anndata.copy()
        )
        return subset

    def __len__(self) -> int:
        """Number of (augmented) samples in the dataset."""
        return len(self.index_manager.augmented_indices)

    def _get_target(self, index: str) -> np.ndarray:
        """Get target for a given index."""
        y_index = self.index_map[index]
        return (
            self.anndata.X[:, y_index].toarray().flatten()
            if self.compressed
            else self.anndata.X[:, y_index]
        )

    def __getitem__(self, idx: int) -> tuple[str, np.ndarray]:
        """Return sequence and target for a given index."""
        augmented_index = self.index_manager.augmented_indices[idx]
        original_index = self.index_manager.augmented_indices_map[augmented_index]

        strand = "-" if augmented_index.endswith(":-") else "+"

        # stochastic shift
        if self.max_stochastic_shift > 0:
            shift = np.random.randint(
                -self.max_stochastic_shift, self.max_stochastic_shift + 1
            )
            x = self.sequence_loader.get_sequence(original_index, strand, shift)
        else:
            x = self.sequence_loader.get_sequence(original_index, strand)

        # random reverse complement (always is done in the sequence loader)
        if self.random_reverse_complement and np.random.rand() < 0.5:
            x = self.sequence_loader._reverse_complement(x)

        y = self._get_target(original_index)
        return x, y

    def __call__(self):
        """Generator for the dataset."""
        for i in range(len(self)):
            yield self.__getitem__(i)

        if i == (len(self) - 1):
            if self.shuffle:
                self.index_manager.shuffle_indices()

    def __repr__(self) -> str:
        """Representation of the dataset."""
        return f"AnnDataset(anndata_shape={self.anndata.shape}, n_samples={len(self)}, num_outputs={self.num_outputs}, split={self.split}, in_memory={self.in_memory})"
