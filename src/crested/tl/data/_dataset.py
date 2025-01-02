"""Dataset class for combining genome files and AnnData objects."""

from __future__ import annotations

import os
import re

import numpy as np
from anndata import AnnData
from loguru import logger
from scipy.sparse import spmatrix
from tqdm import tqdm

from crested._genome import Genome
from crested.utils import one_hot_encode_sequence


def _flip_region_strand(region: str) -> str:
    """Reverse the strand of a region."""
    strand_reverser = {"+": "-", "-": "+"}
    return region[:-1] + strand_reverser[region[-1]]


def _check_strandedness(region: str) -> bool:
    """Check the strandedness of a region, raising an error if the formatting isn't recognised."""
    if re.fullmatch(r".+:\d+-\d+:[-+]", region):
        return True
    elif re.fullmatch(r".+:\d+-\d+", region):
        return False
    else:
        raise ValueError(
            f"Region {region} was not recognised as a valid coordinate set (chr:start-end or chr:start-end:strand)."
            "If provided, strand must be + or -."
        )


def _deterministic_shift_region(
    region: str, stride: int = 50, n_shifts: int = 2
) -> list[str]:
    """
    Shift each region by a deterministic stride to each side. Will increase the number of regions by n_shifts times two.

    This is a legacy function, it's recommended to use stochastic shifting instead.
    """
    new_regions = []
    chrom, start_end, strand = region.split(":")
    start, end = map(int, start_end.split("-"))
    for i in range(-n_shifts, n_shifts + 1):
        new_start = start + i * stride
        new_end = end + i * stride
        new_regions.append(f"{chrom}:{new_start}-{new_end}:{strand}")
    return new_regions


class SequenceLoader:
    """
    Load sequences from a genome file.

    Options for reverse complementing and stochastic shifting are available.

    Parameters
    ----------
    genome
        Genome instance.
    in_memory
        If True, the sequences of supplied regions will be loaded into memory.
    always_reverse_complement
        If True, all sequences will be augmented with their reverse complement.
        Doubles the dataset size.
    max_stochastic_shift
        Maximum stochastic shift (n base pairs) to apply randomly to each sequence.
    regions
        List of regions to load into memory. Required if in_memory is True.
    """

    def __init__(
        self,
        genome: Genome,
        in_memory: bool = False,
        always_reverse_complement: bool = False,
        deterministic_shift: bool = False,
        max_stochastic_shift: int = 0,
        regions: list[str] | None = None,
    ):
        """Initialize the SequenceLoader with the provided genome file and options."""
        self.genome = genome.fasta
        self.chromsizes = genome.chrom_sizes
        self.in_memory = in_memory
        self.always_reverse_complement = always_reverse_complement
        self.deterministic_shift = deterministic_shift
        self.max_stochastic_shift = max_stochastic_shift
        self.sequences = {}
        self.complement = str.maketrans("ACGT", "TGCA")
        self.regions = regions
        if self.in_memory:
            self._load_sequences_into_memory(self.regions)

    def _load_sequences_into_memory(self, regions: list[str]):
        """Load all sequences into memory (dict)."""
        logger.info("Loading sequences into memory...")
        # Check region formatting
        stranded = _check_strandedness(regions[0])

        for region in tqdm(regions):
            # Make region stranded if not
            if not stranded:
                strand = "+"
                region = f"{region}:{strand}"
                if region[-4] == ":":
                    raise ValueError(
                        f"You are double-adding strand ids to your region {region}. Check if all regions are stranded or unstranded."
                    )

            # Add deterministic shift regions
            if self.deterministic_shift:
                regions = _deterministic_shift_region(region)
            else:
                regions = [region]

            for region in regions:
                # Parse region
                chrom, start_end, strand = region.split(":")
                start, end = map(int, start_end.split("-"))

                # Add region to self.sequences
                extended_sequence = self._get_extended_sequence(
                    chrom, start, end, strand
                )
                self.sequences[region] = extended_sequence

                # Add reverse-complemented region to self.sequences if always_reverse_complement
                if self.always_reverse_complement:
                    self.sequences[
                        _flip_region_strand(region)
                    ] = self._reverse_complement(extended_sequence)

    def _get_extended_sequence(
        self, chrom: str, start: int, end: int, strand: str
    ) -> str:
        """Get sequence from genome file, extended for stochastic shifting."""
        extended_start = max(0, start - self.max_stochastic_shift)
        extended_end = extended_start + (end - start) + (self.max_stochastic_shift * 2)

        if self.chromsizes and chrom in self.chromsizes:
            chrom_size = self.chromsizes[chrom]
            if extended_end > chrom_size:
                extended_start = chrom_size - (
                    end - start + self.max_stochastic_shift * 2
                )
                extended_end = chrom_size

        seq = self.genome.fetch(chrom, extended_start, extended_end).upper()
        if strand == "-":
            seq = self._reverse_complement(seq)
        return seq

    def _reverse_complement(self, sequence: str) -> str:
        """Reverse complement a sequence."""
        return sequence.translate(self.complement)[::-1]

    def get_sequence(
        self, region: str, stranded: bool | None = None, shift: int = 0
    ) -> str:
        """
        Get sequence for a region, strand, and shift from memory or fasta.

        If no strand is given in region or strand, assumes positive strand.

        Parameters
        ----------
        region
            Region to get the sequence for. Either (chr:start-end) or (chr:start-end:strand).
        stranded
            Whether the input data is stranded. Default (None) infers from sequence (at a computational cost).
            If not stranded, positive strand is assumed.
        shift:
            Shift of the sequence within the extended sequence, for use with the stochastic shift mechanism.

        Returns
        -------
        The DNA sequence, as a string.
        """
        if stranded is None:
            stranded = _check_strandedness(region)
        if not stranded:
            region = f"{region}:+"
        # Parse region
        chrom, start_end, strand = region.split(":")
        start, end = map(int, start_end.split("-"))

        # Get extended sequence
        if self.in_memory:
            sequence = self.sequences[region]
        else:
            sequence = self._get_extended_sequence(chrom, start, end, strand)

        # Extract from extended sequence
        start_idx = self.max_stochastic_shift + shift
        end_idx = start_idx + (end - start)
        sub_sequence = sequence[start_idx:end_idx]

        # Pad with Ns if sequence is shorter than expected
        if len(sub_sequence) < (end - start):
            if strand == "+":
                sub_sequence = sub_sequence.ljust(end - start, "N")
            else:
                sub_sequence = sub_sequence.rjust(end - start, "N")

        return sub_sequence


class IndexManager:
    """
    Manage indices for the dataset.

    Augments indices with strand information if always reverse complement.

    Parameters
    ----------
    indices
        List of indices in format "chr:start-end" or "chr:start-end:strand".
    always_reverse_complement
        If True, all sequences will be augmented with their reverse complement.
    deterministic_shift
        If True, each region will be shifted twice with stride 50bp to each side.
    """

    def __init__(
        self,
        indices: list[str],
        always_reverse_complement: bool,
        deterministic_shift: bool = False,
    ):
        """Initialize the IndexManager with the provided indices."""
        self.indices = indices
        self.always_reverse_complement = always_reverse_complement
        self.deterministic_shift = deterministic_shift
        self.augmented_indices, self.augmented_indices_map = self._augment_indices(
            indices
        )

    def shuffle_indices(self):
        """Shuffle indices. Managed by wrapping class AnnDataLoader."""
        np.random.shuffle(self.augmented_indices)

    def _augment_indices(self, indices: list[str]) -> tuple[list[str], dict[str, str]]:
        """Augment indices with strand information. Necessary if always reverse complement to map sequences back to targets."""
        augmented_indices = []
        augmented_indices_map = {}
        for region in indices:
            if not _check_strandedness(
                region
            ):  # If slow, can use AnnDataset stranded argument - but this validates every region's formatting as well
                stranded_region = f"{region}:+"
            else:
                stranded_region = region

            if self.deterministic_shift:
                shifted_regions = _deterministic_shift_region(stranded_region)
                for shifted_region in shifted_regions:
                    augmented_indices.append(shifted_region)
                    augmented_indices_map[shifted_region] = region
                    if self.always_reverse_complement:
                        augmented_indices.append(_flip_region_strand(shifted_region))
                        augmented_indices_map[
                            _flip_region_strand(shifted_region)
                        ] = region
            else:
                augmented_indices.append(stranded_region)
                augmented_indices_map[stranded_region] = region
                if self.always_reverse_complement:
                    augmented_indices.append(_flip_region_strand(stranded_region))
                    augmented_indices_map[_flip_region_strand(stranded_region)] = region
        return augmented_indices, augmented_indices_map



class AnnDataset(BaseClass):
    """
    Dataset class for combining genome files and AnnData objects.

    Called by the by the AnnDataModule class.

    Parameters
    ----------
    anndata
        AnnData object containing the data.
    genome
        Genome instance
    split
        'train', 'val', or 'test' split column in anndata.var.
    in_memory
        If True, the train and val sequences will be loaded into memory.
    random_reverse_complement
        If True, the sequences will be randomly reverse complemented during training.
    always_reverse_complement
        If True, all sequences will be augmented with their reverse complement during training.
    max_stochastic_shift
        Maximum stochastic shift (n base pairs) to apply randomly to each sequence during training.
    deterministic_shift
        If true, each region will be shifted twice with stride 50bp to each side.
        This is our legacy shifting, we recommend using max_stochastic_shift instead.
    obs_columns
        Columns in obs that will be added to the dataset.
    obsm_columns
        Keys in obsm that will be added to the dataset.
    varp_columns
        Keys in varp that will be added to the dataset.
    """

    def __init__(
        self,
        anndata: AnnData,
        genome: Genome,
        split: str = None,
        in_memory: bool = True,
        random_reverse_complement: bool = False,
        always_reverse_complement: bool = False,
        max_stochastic_shift: int = 0,
        deterministic_shift: bool = False,
        obs_columns: list[str] | None = None,   # multiple obs columns
        obsm_keys: list[str] | None = None,     # multiple obsm keys
        varp_keys: list[str] | None = None,     # multiple varp keys
    ):

        """Initialize the dataset with the provided AnnData object and options."""
        self.anndata = self._split_anndata(anndata, split)
        self.split = split
        self.indices = list(self.anndata.var_names)
        self.in_memory = in_memory
        self.compressed = isinstance(self.anndata.X, spmatrix)
        self.index_map = {index: i for i, index in enumerate(self.indices)}
        self.num_outputs = self.anndata.X.shape[0]
        self.random_reverse_complement = random_reverse_complement
        self.max_stochastic_shift = max_stochastic_shift
        self.shuffle = False  # managed by wrapping class AnnDataLoader
        self.obs_columns = obs_columns if obs_columns is not None else []
        self.obsm_keys = obsm_keys if obsm_keys is not None else []
        self.varp_keys = varp_keys if varp_keys is not None else []
    
        # Validate and store obs data
        self.obs_data = {}
        for col in self.obs_columns:
            if col not in anndata.obs:
                raise ValueError(f"obs column '{col}' not found.")
            # Convert categorical to integer codes if needed
            if pd.api.types.is_categorical_dtype(anndata.obs[col]):
                self.obs_data[col] = anndata.obs[col].cat.codes.values
            else:
                self.obs_data[col] = anndata.obs[col].values
    
        # Validate and store obsm data
        self.obsm_data = {}
        for key in self.obsm_keys:
            if key not in anndata.obsm:
                raise ValueError(f"obsm key '{key}' not found.")
            mat = anndata.obsm[key]
            if mat.shape[0] != anndata.n_obs:
                raise ValueError(f"Dimension mismatch for obsm key '{key}'.")
            self.obsm_data[key] = mat
            
        # Validate and store varp data
        self.varp_data = {}
        for key in self.varp_keys:
            if key not in anndata.varp:
                raise ValueError(f"varp key '{key}' not found.")
            mat = anndata.varp[key]
            if mat.shape[0] != anndata.n_var:
                raise ValueError(f"Dimension mismatch for varp key '{key}'.")
            self.varp_data[key] = mat

        
        # Check region formatting
        stranded = _check_strandedness(self.indices[0])
        if stranded and (always_reverse_complement or random_reverse_complement):
            logger.info(
                "Setting always_reverse_complement=True or random_reverse_complement=True with stranded data.",
                "This means both strands are used when training and the strand information is effectively disregarded.",
            )

        self.sequence_loader = SequenceLoader(
            genome,
            in_memory=in_memory,
            always_reverse_complement=always_reverse_complement,
            deterministic_shift=deterministic_shift,
            max_stochastic_shift=max_stochastic_shift,
            regions=self.indices,
        )
        self.index_manager = IndexManager(
            self.indices,
            always_reverse_complement=always_reverse_complement,
            deterministic_shift=deterministic_shift,
        )
        self.seq_len = len(
            self.sequence_loader.get_sequence(self.indices[0], stranded=stranded)
        )
        
        self.augmented_probs = None
        if "sample_prob" in anndata.var.columns:
            # 1) Extract raw sample_prob from adata.var
            probs = anndata.var["sample_prob"].values.astype(float)
            # 2) Ensure no negative values
            probs = np.clip(probs, 0, None)

            # 3) For each augmented index, set unnormalized probability
            self.augmented_probs = np.empty(len(self.index_manager.augmented_indices), dtype=float)
            for i, aug_region in enumerate(self.index_manager.augmented_indices):
                original_region = self.index_manager.augmented_indices_map[aug_region]
                var_idx = self.index_map[original_region]
                self.augmented_probs[i] = probs[var_idx]
        else:
            # If no sample_prob, we might default to 1.0 for each region
            # or simply None to indicate uniform sampling
            self.augmented_probs = None

    
    @staticmethod
    def _split_anndata(anndata: AnnData, split: str) -> AnnData:
        """Return subset of anndata based on a given split column."""
        if split:
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
        """Get number of (augmented) samples in the dataset."""
        return len(self.index_manager.augmented_indices)

    def _get_target(self, index: str) -> np.ndarray:
        """Get target for a given index."""
        y_index = self.index_map[index]
        return (
            self.anndata.X[:, y_index].toarray().flatten()
            if self.compressed
            else self.anndata.X[:, y_index]
        )

    def __getitem__(self, idx: int) -> dict:
        """Return sequence and target for a given index."""
        augmented_index = self.index_manager.augmented_indices[idx]
        original_index = self.index_manager.augmented_indices_map[augmented_index]
    
        # Get sequence and target as before
        shift = 0
        if self.max_stochastic_shift > 0:
            shift = np.random.randint(-self.max_stochastic_shift, self.max_stochastic_shift + 1)
    
        x = self.sequence_loader.get_sequence(augmented_index, stranded=True, shift=shift)
        x = one_hot_encode_sequence(x, expand_dim=False)
        y = self._get_target(original_index)
    
        # Random reverse complement if needed
        if self.random_reverse_complement and np.random.rand() < 0.5:
            x = self.sequence_loader._reverse_complement(x)
            x = one_hot_encode_sequence(x, expand_dim=False)
    
        item = {
            "sequence": x,
            "y": y,
        }
    
        # Add obsmp columns directly to the dictionary
        for col in self.obs_columns:
            item[col] = self.obs_data[col]
    
        for key in self.obsm_keys:
            item[key] = self.obsm_data[key]
            
        for key in self.varp_keys:
            item[key] = self.varp_data[key][idx]#.todense()

        return item

    def __call__(self):
        """Call generator for the dataset."""
        for i in range(len(self)):
            if i == 0:
                if self.shuffle:
                    self.index_manager.shuffle_indices()
            yield self.__getitem__(i)

    def __repr__(self) -> str:
        """Get string representation of the dataset."""
        return f"AnnDataset(anndata_shape={self.anndata.shape}, n_samples={len(self)}, num_outputs={self.num_outputs}, split={self.split}, in_memory={self.in_memory})"

class MetaAnnDataset:
    """
    Combines multiple AnnDataset objects into a single dataset,
    merging all their (augmented_index, probability) pairs into one global list.

    We do a final normalization across all sub-datasets so that
    sample_prob from each dataset is treated as an unnormalized weight.
    """

    def __init__(self, datasets: list[AnnDataset]):
        """
        Parameters
        ----------
        datasets : list of AnnDataset
            Each AnnDataset is for a different species or annotation set.
        """
        if not datasets:
            raise ValueError("No AnnDataset provided to MetaAnnDataset.")

        self.datasets = datasets

        # global_indices will store tuples of (dataset_idx, local_idx)
        # global_probs will store the merged, unnormalized probabilities
        self.global_indices = []
        self.global_probs = []

        for ds_idx, ds in enumerate(datasets):
            ds_len = len(ds.index_manager.augmented_indices)
            if ds_len == 0:
                continue

            # If the dataset has augmented_probs, we use them as unnormalized weights
            # If not, fallback to 1.0 for each region
            if ds.augmented_probs is not None:
                for local_i in range(ds_len):
                    self.global_indices.append((ds_idx, local_i))
                    self.global_probs.append(ds.augmented_probs[local_i])
            else:
                for local_i in range(ds_len):
                    self.global_indices.append((ds_idx, local_i))
                    self.global_probs.append(1.0)

        # Convert to numpy arrays
        self.global_indices = np.array(self.global_indices, dtype=object)
        self.global_probs = np.array(self.global_probs, dtype=float)

        # Normalize across the entire set
        total = self.global_probs.sum()
        if total > 0:
            self.global_probs /= total
        else:
            # fallback: uniform if everything is zero
            n = len(self.global_probs)
            if n > 0:
                self.global_probs.fill(1.0 / n)

    def __len__(self):
        """
        The total number of augmented indices across all sub-datasets.
        """
        return len(self.global_indices)

    def __getitem__(self, global_idx: int):
        """
        A DataLoader or sampler will pass a global_idx in [0..len(self)-1].
        We map that to (dataset_idx, local_i) and call the sub-dataset's __getitem__.
        """
        ds_idx, local_i = self.global_indices[global_idx]
        ds_idx = int(ds_idx)
        local_i = int(local_i)
        return self.datasets[ds_idx][local_i]

    def __repr__(self):
        return (f"MetaAnnDataset(num_datasets={len(self.datasets)}, "
                f"total_augmented_indices={len(self.global_indices)})")

