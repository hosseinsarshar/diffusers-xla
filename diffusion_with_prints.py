import time
import datasets
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint

from torchvision import transforms
from transformers import CLIPTextModel, CLIPTokenizer

from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.training_utils import compute_snr

import torch_xla
import torch_xla.core.xla_model as xm
import torch_xla.debug.profiler as xp
import torch_xla.distributed.parallel_loader as pl
import torch_xla.runtime as xr
import torch_xla.distributed.spmd as xs

PROFILE_DIR=os.environ.get('PROFILE_DIR', None)
CACHE_DIR = os.environ.get('CACHE_DIR', None)
if CACHE_DIR:
    xr.initialize_cache(CACHE_DIR, readonly=False)
xr.use_spmd()
DATASET_NAME_MAPPING = {
    "lambdalabs/naruto-blip-captions": ("image", "text"),
}

class TrainSD():

    def __init__(
        self,
        vae,
        weight_dtype,
        device,
        noise_scheduler,
        unet,
        optimizer,
        text_encoder,
        dataloader,
        args,
    ):
        self.vae = vae
        self.weight_dtype = weight_dtype
        self.device = device
        self.noise_scheduler = noise_scheduler
        self.unet = unet
        self.optimizer = optimizer
        self.text_encoder = text_encoder
        self.args = args
        self.mesh = xs.get_global_mesh()
        self.dataloader = iter(dataloader)
        self.global_step = 0

    def run_optimizer(self):
        self.optimizer.step()

    def start_training(self):
        dataloader_exception = False
        measure_start_step = 10
        assert measure_start_step < self.args.max_train_steps
        total_time = 0
        print("Step 17.0")
        for step in range(0, self.args.max_train_steps):
            try:
                print("Step 17.1: ", step)
                batch = next(self.dataloader)
                print("Step 17.2: ", step)
            except Exception as e:
                dataloader_exception = True
                print(e)
                break
            if step ==  measure_start_step and PROFILE_DIR is not None:
                print("Step 17.3: ", step)
                xm.wait_device_ops()
                xp.trace_detached('localhost:9012', PROFILE_DIR, duration_ms=args.profile_duration)
                last_time = time.time()     
            loss = self.step_fn(batch["pixel_values"], batch["input_ids"])
            self.global_step += 1
        
        print("Step 17.4")
        xm.mark_step()
        print("Step 17.5")
        if not dataloader_exception:
            print("Step 17.6")
            xm.wait_device_ops()
            print("Step 17.7")
            total_time = time.time() - last_time
            print(f"Average step time: {total_time/(self.args.max_train_steps-measure_start_step)}")
        else:
            print("dataloader exception happen, skip result")
            return

    def step_fn(
        self,
        pixel_values,
        input_ids,
        ):
        with xp.Trace("model.forward"):
            self.optimizer.zero_grad()
            latents = self.vae.encode(pixel_values).latent_dist.sample()
            latents = latents * self.vae.config.scaling_factor
            noise = torch.randn_like(latents).to(self.device, dtype=self.weight_dtype)
            bsz = latents.shape[0]
            timesteps = torch.randint(
                0,
                self.noise_scheduler.config.num_train_timesteps,
                (bsz,),
                device=latents.device,
            )
            timesteps = timesteps.long()

            noisy_latents = self.noise_scheduler.add_noise(latents, noise, timesteps)
            encoder_hidden_states = self.text_encoder(input_ids, return_dict=False)[0]
            if self.args.prediction_type is not None:
                # set prediction_type of scheduler if defined
                self.noise_scheduler.register_to_config(prediction_type=self.args.prediction_type)

            if self.noise_scheduler.config.prediction_type == "epsilon":
                target = noise
            elif self.noise_scheduler.config.prediction_type == "v_prediction":
                target = self.noise_scheduler.get_velocity(latents, noise, timesteps)
            else:
                raise ValueError(
                    f"Unknown prediction type {self.noise_scheduler.config.prediction_type}"
                )
            model_pred = self.unet(
                noisy_latents, timesteps, encoder_hidden_states, return_dict=False
            )[0]
        with xp.Trace("model.backward"):
            if self.args.snr_gamma is None:
                loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
            else:
                # Compute loss-weights as per Section 3.4 of https://arxiv.org/abs/2303.09556.
                # Since we predict the noise instead of x_0, the original formulation is slightly changed.
                # This is discussed in Section 4.2 of the same paper.
                snr = compute_snr(self.noise_scheduler, timesteps)
                mse_loss_weights = torch.stack(
                    [snr, self.args.snr_gamma * torch.ones_like(timesteps)], dim=1
                ).min(dim=1)[0]
                if self.noise_scheduler.config.prediction_type == "epsilon":
                    mse_loss_weights = mse_loss_weights / snr
                elif self.noise_scheduler.config.prediction_type == "v_prediction":
                    mse_loss_weights = mse_loss_weights / (snr + 1)

                loss = F.mse_loss(model_pred.float(), target.float(), reduction="none")
                loss = loss.mean(dim=list(range(1, len(loss.shape)))) * mse_loss_weights
                loss = loss.mean()
            loss.backward()
        with xp.Trace("optimizer_step"):
            self.run_optimizer()
        return loss

def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--profile_duration", type=int, default=10000, help="Profile duration in ms"
    )
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--variant",
        type=str,
        default=None,
        help="Variant of the model files of the pretrained model identifier from huggingface.co/models, 'e.g.' fp16",
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that 🤗 Datasets can understand."
        ),
    )
    parser.add_argument(
        "--train_data_dir",
        type=str,
        default=None,
        help=(
            "A folder containing the training data. Folder contents must follow the structure described in"
            " https://huggingface.co/docs/datasets/image_dataset#imagefolder. In particular, a `metadata.jsonl` file"
            " must exist to provide the captions for the images. Ignored if `dataset_name` is specified."
        ),
    )
    parser.add_argument(
        "--image_column", type=str, default="image", help="The column of the dataset containing an image."
    )
    parser.add_argument(
        "--caption_column",
        type=str,
        default="text",
        help="The column of the dataset containing a caption or a list of captions.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="sd-model-finetuned",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--center_crop",
        default=False,
        action="store_true",
        help=(
            "Whether to center crop the input images to the resolution. If not set, the images will be randomly"
            " cropped. The images will be resized to the resolution first before cropping."
        ),
    )
    parser.add_argument(
        "--random_flip",
        action="store_true",
        help="whether to randomly flip images horizontally",
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=16, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--snr_gamma",
        type=float,
        default=None,
        help="SNR weighting gamma to be used if rebalancing the loss. Recommended value is 5.0. "
        "More details here: https://arxiv.org/abs/2303.09556.",
    )
    parser.add_argument(
        "--non_ema_revision",
        type=str,
        default=None,
        required=False,
        help=(
            "Revision of pretrained non-ema model identifier. Must be a branch, tag or git identifier of the local or"
            " remote repository specified with --pretrained_model_name_or_path."
        ),
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    parser.add_argument(
        "--loader_prefetch_size",
        type=int,
        default=1,
        help=(
            "Number of subprocesses to use for data loading to cpu."
        ),
    )
    parser.add_argument(
        "--loader_prefetch_factor",
        type=int,
        default=2,
        help=(
            "Number of batches loaded in advance by each worker."
        ),
    )
    parser.add_argument(
        "--device_prefetch_size",
        type=int,
        default=1,
        help=(
            "Number of subprocesses to use for data loading to tpu from cpu. "
        ),
    )
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
    parser.add_argument(
        "--prediction_type",
        type=str,
        default=None,
        help="The prediction_type that shall be used for training. Choose between 'epsilon' or 'v_prediction' or leave `None`. If left to `None` the default prediction type of the scheduler: `noise_scheduler.config.prediction_type` is chosen.",
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "bf16"],
        help=(
            "Whether to use mixed precision. Bf16 requires PyTorch >= 1.10"
        ),
    )

    args = parser.parse_args()

    # default to using the same revision for the non-ema model if not specified
    if args.non_ema_revision is None:
        args.non_ema_revision = args.revision

    return args

def setup_optimizer(unet, args):
    optimizer_cls = torch.optim.AdamW
    return optimizer_cls(
        unet.parameters(),
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
        foreach=True,
    )

def load_dataset(args):
    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        dataset = datasets.load_dataset(
            args.dataset_name,
            cache_dir=args.cache_dir,
            data_dir=args.train_data_dir,
        )
    else:
        data_files = {}
        if args.train_data_dir is not None:
            data_files["train"] = os.path.join(args.train_data_dir, "**")
        dataset = datasets.load_dataset(
            "imagefolder",
            data_files=data_files,
            cache_dir=args.cache_dir,
        )
    return dataset

def get_column_names(dataset, args):
    column_names = dataset["train"].column_names

    dataset_columns = DATASET_NAME_MAPPING.get(args.dataset_name, None)
    if args.image_column is None:
        image_column = dataset_columns[0] if dataset_columns is not None else column_names[0]
    else:
        image_column = args.image_column
        if image_column not in column_names:
            raise ValueError(
                f"--image_column' value '{args.image_column}' needs to be one of: {', '.join(column_names)}"
            )
    if args.caption_column is None:
        caption_column = dataset_columns[1] if dataset_columns is not None else column_names[1]
    else:
        caption_column = args.caption_column
        if caption_column not in column_names:
            raise ValueError(
                f"--caption_column' value '{args.caption_column}' needs to be one of: {', '.join(column_names)}"
            )
    return image_column, caption_column


def main(args):

    args = parse_args()

    server = xp.start_server(9012)

    num_devices = xr.global_runtime_device_count()
    mesh = xs.get_1d_mesh('data')
    xs.set_global_mesh(mesh)

    text_encoder = CLIPTextModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=args.revision,
        variant=args.variant,
    )
    vae = AutoencoderKL.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="vae",
        revision=args.revision,
        variant=args.variant,
    )

    unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="unet",
        revision=args.non_ema_revision,
    )
    noise_scheduler = DDPMScheduler.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="scheduler"
    )
    tokenizer = CLIPTokenizer.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="tokenizer",
        revision=args.revision,
    )

    from torch_xla.distributed.fsdp.utils import apply_xla_patch_to_nn_linear
    unet = apply_xla_patch_to_nn_linear(unet, xs.xla_patched_nn_linear_forward)

    vae.requires_grad_(False)
    text_encoder.requires_grad_(False)
    unet.train()

    # For mixed precision training we cast all non-trainable weights (vae,
    # non-lora text_encoder and non-lora unet) to half-precision
    # as these weights are only used for inference, keeping weights in full
    # precision is not required.
    weight_dtype = torch.float32
    if args.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16
    print("Step 0")
    device = xm.xla_device()
    print("Step 1")
    # Move text_encode and vae to gpu and cast to weight_dtype
    text_encoder = text_encoder.to(device, dtype=weight_dtype)
    vae = vae.to(device, dtype=weight_dtype)
    unet = unet.to(device, dtype=weight_dtype)
    optimizer = setup_optimizer(unet, args)
    vae.requires_grad_(False)
    text_encoder.requires_grad_(False)
    print("Step 2")
    unet.train()
    print("Step 3")

    dataset = load_dataset(args)
    print("Step 4")

    image_column, caption_column = get_column_names(dataset, args)
    print("Step 5")

    def tokenize_captions(examples, is_train=True):
        captions = []
        for caption in examples[caption_column]:
            if isinstance(caption, str):
                captions.append(caption)
            elif isinstance(caption, (list, np.ndarray)):
                # take a random caption if there are multiple
                captions.append(random.choice(caption) if is_train else caption[0])
            else:
                raise ValueError(
                    f"Caption column `{caption_column}` should contain either strings or lists of strings."
                )
        inputs = tokenizer(
            captions,
            max_length=tokenizer.model_max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        return inputs.input_ids
    print("Step 6")

    train_transforms = transforms.Compose(
        [
            transforms.Resize(
                args.resolution, interpolation=transforms.InterpolationMode.BILINEAR
            ),
            (
                transforms.CenterCrop(args.resolution)
                if args.center_crop
                else transforms.RandomCrop(args.resolution)
            ),
            (
                transforms.RandomHorizontalFlip()
                if args.random_flip
                else transforms.Lambda(lambda x: x)
            ),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    def preprocess_train(examples):
        images = [image.convert("RGB") for image in examples[image_column]]
        examples["pixel_values"] = [train_transforms(image) for image in images]
        examples["input_ids"] = tokenize_captions(examples)
        return examples
    print("Step 7")

    train_dataset = dataset["train"]
    train_dataset.set_format('torch')
    train_dataset.set_transform(preprocess_train)
    print("Step 8")

    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).to(
            weight_dtype
        )
        input_ids = torch.stack([example["input_ids"] for example in examples])
        return {"pixel_values": pixel_values, "input_ids": input_ids}

    print("Step 9")
    g = torch.Generator()
    print("Step 10")
    g.manual_seed(xr.host_index())
    sampler = torch.utils.data.RandomSampler(
        train_dataset, replacement=True, num_samples=int(1e10), generator=g
    )
    print("Step 11")
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        sampler=sampler,
        collate_fn=collate_fn,
        num_workers=args.dataloader_num_workers,
        batch_size=args.train_batch_size,
        prefetch_factor=args.loader_prefetch_factor,
    )

    print("Step 12")
    train_dataloader = pl.MpDeviceLoader(
        train_dataloader,
        device,
        input_sharding={
            "pixel_values": xs.ShardingSpec(
                mesh, ("data", None, None, None), minibatch=True
            ),
            "input_ids": xs.ShardingSpec(mesh, ("data", None), minibatch=True),
        },
        loader_prefetch_size=args.loader_prefetch_size,
        device_prefetch_size=args.device_prefetch_size,
    )

    print("Step 13")
    num_hosts = xr.process_count()
    print("Step 14")
    num_devices_per_host = num_devices // num_hosts
    if xm.is_master_ordinal():
        print("***** Running training *****")
        print(f"Instantaneous batch size per device = {args.train_batch_size // num_devices_per_host }")
        print(
            f"Total train batch size (w. parallel, distributed & accumulation) = {args.train_batch_size * num_hosts}"
        )
        print(f"  Total optimization steps = {args.max_train_steps}")

    print("Step 15")
    trainer = TrainSD(vae=vae,
                      weight_dtype=weight_dtype,
                      device=device,
                      noise_scheduler=noise_scheduler,
                      unet=unet,
                      optimizer=optimizer,
                      text_encoder=text_encoder,
                      dataloader=train_dataloader,
                      args=args)

    print("Step 16")
    trainer.start_training()
    unet = trainer.unet.to("cpu")
    vae = trainer.vae.to("cpu")
    text_encoder = trainer.text_encoder.to("cpu")
    print("Step 17")

    pipeline = StableDiffusionPipeline.from_pretrained(
        args.pretrained_model_name_or_path,
        text_encoder=text_encoder,
        vae=vae,
        unet=unet,
        revision=args.revision,
        variant=args.variant,
    )
    print("Step 18")
    pipeline.save_pretrained(args.output_dir)


if __name__ == "__main__":
    args = parse_args()
    main(args)