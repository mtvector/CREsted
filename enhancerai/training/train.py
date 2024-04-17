import tensorflow as tf
import yaml
import os
import shutil
import argparse
from datetime import datetime

import wandb
from wandb.keras import WandbMetricsLogger, WandbCallback

from models.zoo import simple_convnet, chrombpnet, basenji, deeptopiccnn

from utils.metrics import (
    get_lr_metric,
    PearsonCorrelation,
    LogMSEPerClassCallback,
    SpearmanCorrelationPerClass,
    PearsonCorrelationLog,
    ZeroPenaltyMetric,
    ConcordanceCorrelationCoefficient,
)
from utils.loss import CustomLoss, CustomLossV2, CustomLossMSELogV2_
from utils.augment import complement_base
from utils.dataloader import CustomDataset


def parse_arguments() -> argparse.Namespace:
    """Parse required command line arguments."""
    parser = argparse.ArgumentParser(description="Train a model.")
    parser.add_argument(
        "-g",
        "--genome_fasta_file",
        type=str,
        help="Path to genome FASTA file",
        default="data/raw/genome.fa",
    )
    parser.add_argument(
        "-b",
        "--bed_file",
        type=str,
        help="Path to BED file",
        default="data/processed/consensus_peaks_inputs.bed",
    )
    parser.add_argument(
        "-t",
        "--targets_file",
        type=str,
        help="Path to targets file (npz format). Deeppeak or deeptopic.",
        required=True,
    )
    parser.add_argument(
        "-m",
        "--cell_mapping_file",
        type=str,
        help="Path to cell type mapping file",
        default="data/processed/cell_type_mapping.tsv",  ## TO DO: Fix cell type mapping file
    )
    parser.add_argument(
        "-o",
        "--output_dir",
        type=str,
        help="Path to output directory",
        default="checkpoints/",
    )
    parser.add_argument(
        "-c",
        "--chrom_sizes_file",
        type=str,
        default="data/raw/chrom.sizes",
        help="Path to the chromosome sizes file. Required if --filter_chrom is True.",
    )

    return parser.parse_args()


def _load_chromsizes(chrom_sizes_file: str) -> "dict[str, int]":
    chrom_sizes = {}
    with open(chrom_sizes_file, "r") as sizes:
        for line in sizes:
            chrom, s_size = line.strip().split("\t")[0:2]
            i_size = int(s_size)
            chrom_sizes[chrom] = i_size
    return chrom_sizes


def model_callbacks(
    checkpoint_dir: str,
    task: str,
    patience: int,
    use_wandb: bool,
    profile: bool,
    validation_data=None,
    class_names: list = None,
    val_steps_per_epoch: int = None,
) -> list:
    """Get model callbacks."""
    callbacks = []
    # Checkpoints
    checkpoint = tf.keras.callbacks.ModelCheckpoint(
        os.path.join(checkpoint_dir, "checkpoints", "{epoch:02d}.keras"),
        save_freq="epoch",
        save_weights_only=False,
        save_best_only=False,
    )
    callbacks.append(checkpoint)

    # Early stopping
    if task == "deeppeak":
        early_stop_metric = "val_pearson_correlation"
    elif task == "deeptopic":
        early_stop_metric = "val_auPR"
    early_stop = tf.keras.callbacks.EarlyStopping(
        monitor=early_stop_metric, patience=patience, mode="max"
    )
    callbacks.append(early_stop)

    # Lr reduction
    reduce_lr = tf.keras.callbacks.ReduceLROnPlateau(
        monitor=early_stop_metric,
        factor=0.25,
        patience=int(patience // 2),
        min_lr=0.000001,
        mode="max",
    )
    callbacks.append(reduce_lr)

    # Wandb
    if use_wandb:
        wandb_callback_epoch = WandbMetricsLogger(log_freq="epoch")
        wandb_callback_batch = WandbMetricsLogger(log_freq=10)
        wandb_model_callback = WandbCallback()

        callbacks.append(wandb_callback_epoch)
        callbacks.append(wandb_callback_batch)
        callbacks.append(wandb_model_callback)
        if task == "deeppeak":
            log_mse_per_class_callback = LogMSEPerClassCallback(
                validation_data=validation_data,
                class_names=class_names,
                val_steps=val_steps_per_epoch,
            )
            callbacks.append(log_mse_per_class_callback)

    if profile:
        print("Profiling enabled. Saving to logs/")
        tensorboard_callback = tf.keras.callbacks.TensorBoard(
            log_dir="logs/", histogram_freq=1, profile_batch="10, 60"
        )
        callbacks.append(tensorboard_callback)

    return callbacks


def load_datasets(
    bed_file: str,
    genome_fasta_file: str,
    targets_file: str,
    config: dict,
    batch_size: int,
    checkpoint_dir: str,
    chromsizes: "dict[str, int]",
):
    """Load train & val datasets."""
    # Load data
    split_dict = {"val": config["val"], "test": config["test"]}

    dataset = CustomDataset(
        bed_file,
        genome_fasta_file,
        config["task"],
        targets_file,
        config["deeppeak"]["target"],
        split_dict,
        config["num_classes"],
        config["augment_shift_n_bp"],
        config["fraction_of_data"],
        checkpoint_dir,
        chromsizes,
        config["rev_complement"],
        config["specificity_filtering"],
        config["shift_augmentation"]["use"],
        config["shift_augmentation"]["n_shifts"],
    )

    seq_len = config["seq_len"]

    base_to_int_mapping = {"A": 0, "C": 1, "G": 2, "T": 3}
    table = tf.lookup.StaticHashTable(
        initializer=tf.lookup.KeyValueTensorInitializer(
            keys=tf.constant(list(base_to_int_mapping.keys())),
            values=tf.constant(list(base_to_int_mapping.values()), dtype=tf.int32),
        ),
        default_value=-1,
    )

    @tf.function
    def mapped_function(sequence, target, augment_complement=False):
        if isinstance(sequence, str):
            sequence = tf.constant([sequence])
        elif isinstance(sequence, tf.Tensor) and sequence.ndim == 0:
            sequence = tf.expand_dims(sequence, 0)

        # Define one_hot_encode function using TensorFlow operations
        def one_hot_encode(sequence):
            # Map each base to an integer
            char_seq = tf.strings.unicode_split(sequence, "UTF-8")
            integer_seq = table.lookup(char_seq)
            # One-hot encode the integer sequence
            x = tf.one_hot(integer_seq, depth=4)
            if augment_complement:
                x = complement_base(x)
            return x

        # Apply one_hot_encode to each sequence
        one_hot_sequence = tf.map_fn(
            one_hot_encode,
            sequence,
            fn_output_signature=tf.TensorSpec(shape=(seq_len, 4), dtype=tf.float32),
        )
        one_hot_sequence = tf.squeeze(one_hot_sequence, axis=0)  # remove extra map dim
        return one_hot_sequence, target

    train_data = (
        dataset.subset("train")
        .map(
            lambda seq, tgt: mapped_function(
                seq, tgt, augment_complement=config["augment_complement"]
            ),
            num_parallel_calls=tf.data.AUTOTUNE,
        )
        .shuffle(buffer_size=2048, reshuffle_each_iteration=True)
        .batch(batch_size, drop_remainder=True)
        .repeat(config["epochs"])
        .prefetch(tf.data.AUTOTUNE)
    )

    val_data = (
        dataset.subset("val")
        .map(mapped_function, num_parallel_calls=tf.data.AUTOTUNE)
        .batch(batch_size)
        .repeat(config["epochs"])
        .prefetch(tf.data.AUTOTUNE)
    )

    total_number_of_training_samples = dataset.len("train")
    total_number_of_validation_samples = dataset.len("val")

    return (
        train_data,
        val_data,
        total_number_of_training_samples,
        total_number_of_validation_samples,
    )


def load_model_architecture(task: str, config: dict) -> tf.keras.Model:
    """Load requested model from zoo using the given configuration"""
    model_name = config[task]["model_architecture"]

    options = [
        "basenji",
        "chrombpnet",
        "simple_convnet",
        "deeptopiccnn",
        "deeptopiclstm",
    ]
    if model_name not in options:
        raise ValueError(f"Model {model_name} not supported.")

    model_config = config[model_name]

    if model_name == "basenji":
        model = basenji(
            input_shape=(config["seq_len"], 4),
            output_shape=(1, config["num_classes"]),
            first_activation=model_config["first_activation"],
            activation=model_config["activation"],
            output_activation=model_config["output_activation"],
            first_filters=model_config["first_filters"],
            filters=model_config["filters"],
            first_kernel_size=model_config["first_kernel_size"],
            kernel_size=model_config["kernel_size"],
        )
    elif model_name == "chrombpnet":
        model = chrombpnet(
            input_shape=(config["seq_len"], 4),
            output_shape=(1, config["num_classes"]),
            first_conv_filters=model_config["first_conv_filters"],
            first_conv_filter_size=model_config["first_conv_filter_size"],
            first_conv_pool_size=model_config["first_conv_pool_size"],
            first_conv_activation=model_config["first_conv_activation"],
            first_conv_l2=model_config["first_conv_l2"],
            first_conv_dropout=model_config["first_conv_dropout"],
            n_dil_layers=model_config["n_dil_layers"],
            num_filters=model_config["num_filters"],
            filter_size=model_config["filter_size"],
            activation=model_config["activation"],
            l2=model_config["l2"],
            dropout=model_config["dropout"],
            batch_norm=model_config["batch_norm"],
            dense_bias=model_config["dense_bias"],
        )
    elif model_name == "simple_convnet":
        model = simple_convnet(
            input_shape=(config["seq_len"], 4),
            output_shape=(1, config["num_classes"]),
            num_conv_blocks=model_config["num_conv_blocks"],
            num_dense_blocks=model_config["num_dense_blocks"],
            residual=model_config["residual"],
            first_activation=model_config["first_activation"],
            activation=model_config["activation"],
            output_activation=model_config["output_activation"],
            first_filters=model_config["first_filters"],
            filters=model_config["filters"],
            first_kernel_size=model_config["first_kernel_size"],
            kernel_size=model_config["kernel_size"],
            first_pool_size=model_config["first_pool_size"],
            pool_size=model_config["pool_size"],
            conv_dropout=model_config["conv_dropout"],
            dense_dropout=model_config["dense_dropout"],
            flatten=model_config["flatten"],
            dense_size=model_config["dense_size"],
            bottleneck=model_config["bottleneck"],
        )
    elif model_name == "deeptopiccnn":
        model = deeptopiccnn(
            input_shape=(config["seq_len"], 4),
            output_shape=(1, config["num_classes"]),
            filters=model_config["filters"],
            first_kernel_size=model_config["first_kernel_size"],
            pool_size=model_config["pool_size"],
            dense_out=model_config["dense_out"],
            first_activation=model_config["first_activation"],
            activation=model_config["activation"],
            normalization=model_config["normalization"],
            conv_do=model_config["conv_do"],
            dense_do=model_config["dense_do"],
            pre_dense_do=model_config["pre_dense_do"],
            first_kernel_l2=model_config["first_kernel_l2"],
            kernel_l2=model_config["kernel_l2"],
        )

    return model


def get_compiled_model(task, config):
    pt_model = config["pretrained_model_path"]

    # Model architecture
    if pt_model:
        print(f"Continuing training from pretrained model {pt_model}...")
        model = tf.keras.models.load_model(pt_model, compile=False)
        optimizer = tf.keras.optimizers.Adam(learning_rate=config["TL_learning_rate"])

    else:
        print("Training from scratch...")
        model = load_model_architecture(task, config)
        optimizer = tf.keras.optimizers.Adam(learning_rate=config["learning_rate"])

    # Losses and metrics
    if task == "deeppeak":
        # Losses
        if config["deeppeak"]["cosine_weight"] == "dynamic":
            cos_weight = 100.0
        else:
            cos_weight = 1.0  # default or 'static'

        if config["deeppeak"]["loss"] == "mse_cosine":
            loss = CustomLoss()
        elif config["deeppeak"]["loss"] == "mse_cosine_log":
            loss = CustomLossMSELogV2_(max_weight=cos_weight)
        elif config["deeppeak"]["loss"] == "mse_cosine_nk":
            loss = CustomLossV2(max_weight=cos_weight)
        else:
            loss = CustomLossV2(max_weight=cos_weight)  # default

        # Metrics
        lr_metric = get_lr_metric(optimizer)
        metrics = [
            tf.keras.metrics.MeanAbsoluteError(),
            tf.keras.metrics.MeanSquaredError(),
            tf.keras.metrics.CosineSimilarity(axis=1),
            PearsonCorrelation(),
            SpearmanCorrelationPerClass(num_classes=config["num_classes"]),
            ConcordanceCorrelationCoefficient(),
            PearsonCorrelationLog(),
            ZeroPenaltyMetric(),
            lr_metric,
        ]

    elif task == "deeptopic":
        # Losses
        loss = "binary_crossentropy"

        # Metrics
        metrics = [
            tf.keras.metrics.AUC(
                num_thresholds=200,
                curve="ROC",
                summation_method="interpolation",
                name="auROC",
                thresholds=None,
                multi_label=True,
                label_weights=None,
            ),
            tf.keras.metrics.AUC(
                num_thresholds=200,
                curve="PR",
                summation_method="interpolation",
                name="auPR",
                thresholds=None,
                multi_label=True,
                label_weights=None,
            ),
            tf.keras.metrics.CategoricalAccuracy(),
        ]

    # Compile the model
    model.compile(optimizer=optimizer, loss=loss, metrics=metrics)
    return model


def main(args: argparse.Namespace, config: dict):
    # Init configs and wandb
    now = datetime.now().strftime("%Y-%m-%d_%H:%M")
    task = config["task"]  # deeppeak or deeptopic
    assert task in ["deeptopic", "deeppeak"], "Task must be 'deeptopic' or 'deeppeak'"

    if len(config["run_name"]) > 0:
        run_name = config["run_name"]
    else:
        run_name = now
    if config["wandb"]:
        if config["wandb_project"] == "":
            project_name = f"{task}_{config['project_name']}"
        else:
            project_name = config["wandb_project"]
        run = wandb.init(
            project=project_name,
            config=config,
            name=run_name,
        )
    if int(config["seed"]) > 0:
        tf.random.set_seed(int(config["seed"]))

    if config["output_dir"] == "":
        checkpoint_dir = os.path.join(
            args.output_dir, config["project_name"], config["run_name"]
        )
    else:
        if not os.path.exists(config["output_dir"]):
            raise Exception(f"Output directory {config['output_dir']} does not exist.")
        checkpoint_dir = os.path.join(config["output_dir"], config["run_name"])

    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    elif os.path.exists(checkpoint_dir):
        shutil.rmtree(checkpoint_dir)
        os.makedirs(checkpoint_dir)
    shutil.copyfile("configs/user.yml", os.path.join(checkpoint_dir, "user.yml"))
    shutil.copyfile(
        args.cell_mapping_file, os.path.join(checkpoint_dir, "cell_type_mapping.tsv")
    )

    # Train on GPU
    gpus_found = tf.config.list_physical_devices("GPU")

    strategy = tf.distribute.MirroredStrategy()
    print("Number of replica devices in use: {}".format(strategy.num_replicas_in_sync))
    print("Number of GPUs available: {}".format(len(gpus_found)))

    if config["wandb"]:
        wandb.config.update({"num_gpus_available": len(gpus_found)})
        wandb.config.update({"num_devices_used": strategy.num_replicas_in_sync})

    # Mixed precision (for GPU)
    if config["mixed_precision"]:
        print("WARNING: Mixed precision enabled. Disable on CPU or older GPUs.")
        tf.keras.mixed_precision.set_global_policy("mixed_float16")

    # Load data
    batch_size = (
        config["batch_size"]
        if len(config["pretrained_model_path"]) == 0
        else config["TL_batch_size"]
    )
    print("Batch size for training: " + str(batch_size))

    chromsizes = _load_chromsizes(args.chrom_sizes_file)

    (
        train,
        val,
        total_number_of_training_samples,
        total_number_of_validation_samples,
    ) = load_datasets(
        args.bed_file,
        args.genome_fasta_file,
        args.targets_file,
        config,
        batch_size,
        checkpoint_dir,
        chromsizes,
    )

    if config["wandb"]:
        wandb.config.update(
            {
                "N_train": total_number_of_training_samples,
                "N_val": total_number_of_validation_samples,
            }
        )

    # Initialize the model
    with strategy.scope():
        model = get_compiled_model(task, config)

    # Get callbacks
    class_names = []
    with open(checkpoint_dir + "/cell_type_mapping.tsv", "r") as f:
        for line in f:
            class_names.append(line.strip().split("\t")[1])

    train_steps_per_epoch = total_number_of_training_samples // batch_size
    val_steps_per_epoch = total_number_of_validation_samples // batch_size

    callbacks = model_callbacks(
        checkpoint_dir,
        task,
        config["patience"],
        config["wandb"],
        config["profile"],
        val,
        class_names,
        val_steps_per_epoch,
    )

    print(model.summary())

    # Train the model
    model.fit(
        train,
        steps_per_epoch=train_steps_per_epoch,
        validation_steps=val_steps_per_epoch,
        validation_data=val,
        epochs=config["epochs"],
        callbacks=callbacks,
    )

    if config["wandb"]:
        run.finish()


if __name__ == "__main__":
    # Load args and config
    args = parse_arguments()

    assert os.path.exists(
        "configs/user.yml"
    ), "users.yml file not found. Please run `make copyconfig first`"
    with open("configs/user.yml", "r") as f:
        config = yaml.safe_load(f)

    # Train
    main(args, config)
