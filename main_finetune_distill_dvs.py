# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# --------------------------------------------------------

import argparse
import datetime
import json
import numpy as np
import os
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter

import timm

assert timm.__version__ == "0.3.2"  # version check
from timm.models.layers import trunc_normal_
from timm.data.mixup import Mixup
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy

import util.lr_decay as lrd
import util.misc as misc
from util.datasets import build_dataset
from util.neuromorphic_datasets import build_neuromorphic_dataset
from util.pos_embed import interpolate_pos_embed
from util.misc import NativeScalerWithGradNormCount as NativeScaler
from spike_quan_wrapper import myquan_replace, SNNWrapper

import models_vit
import wandb

from engine_finetune import train_one_epoch, evaluate, train_one_epoch_distill

import warnings

warnings.filterwarnings("ignore", category=UserWarning)


def get_args_parser():
    parser = argparse.ArgumentParser('MAE fine-tuning for image classification', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--print_freq', default=1000, type=int,
                        help='print_frequency')
    parser.add_argument('--accum_iter', default=1, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')
    parser.add_argument('--project_name', default='T-SNN', type=str, metavar='MODEL',
                        help='Name of model to train')

    # Model parameters
    parser.add_argument('--model', default='vit_small_patch16', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--model_teacher', default='vit_base_patch16', type=str, metavar='MODEL',
                        help='Name of model to train')

    parser.add_argument('--input_size', default=224, type=int,
                        help='images input size')
    parser.add_argument('--in_channels', default=18, type=int,
                        help='input channels for neuromorphic datasets')
    parser.add_argument('--encoding_type', default="analog", type=str,
                        help='encoding type for snn')
    parser.add_argument('--time_step', default=2000, type=int,
                        help='time-step for snn')
    parser.add_argument('--drop_path', type=float, default=0.1, metavar='PCT',
                        help='Drop path rate (default: 0.1)')

    # Optimizer parameters
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--layer_decay', type=float, default=0.75,
                        help='layer-wise lr decay from ELECTRA/BEiT')
    parser.add_argument('--act_layer', type=str, default="relu",
                        help='Using ReLU or GELU as activation')
    parser.add_argument('--act_layer_teacher', type=str, default="gelu",
                        help='Using ReLU or GELU as activation for teacher model')

    parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')
    parser.add_argument('--temp', type=float, default=2.0, metavar='T',
                        help='temperature for distillation')

    parser.add_argument('--warmup_epochs', type=int, default=5, metavar='N',
                        help='epochs to warmup LR')

    # Augmentation parameters
    parser.add_argument('--color_jitter', type=float, default=None, metavar='PCT',
                        help='Color jitter factor (enabled only when not using Auto/RandAug)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', action='store_true', default=False,
                        help='Do not random erase first (clean) augmentation split')

    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # * Finetuning params
    parser.add_argument('--finetune', default='',
                        help='finetune from checkpoint')
    parser.add_argument('--pretrain_teacher', default='',
                        help='pretrained teacher model')
    parser.add_argument('--global_pool', action='store_true')
    parser.set_defaults(global_pool=True)
    parser.add_argument('--cls_token', action='store_false', dest='global_pool',
                        help='Use class token instead of global pool for classification')

    # Dataset parameters
    parser.add_argument('--dataset', default='imagenet', type=str,
                        help='dataset name')
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--nb_classes', default=1000, type=int,
                        help='number of the classification types')
    parser.add_argument('--define_params', action='store_true')
    parser.add_argument('--mean', nargs='+', type=float)
    parser.add_argument('--std', nargs='+', type=float)

    parser.add_argument('--output_dir', default='./output_dir',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default='./output_dir',
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--wandb', action='store_true',
                        help='Using wandb or not')
    parser.add_argument('--dist_eval', action='store_true', default=False,
                        help='Enabling distributed evaluation (recommended during training for faster monitor')
    parser.add_argument('--num_workers', default=32, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=True)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    # training mode
    parser.add_argument('--mode', default="ANN", type=str,
                        help='the running mode of the script["ANN", "QANN_PTQ", "QANN_QAT", "SNN"]')

    # LSQ quantization
    parser.add_argument('--level', default=32, type=int,
                        help='the quantization levels')
    parser.add_argument('--neuron_type', default="ST-BIF", type=str,
                        help='neuron type["ST-BIF", "IF"]')

    return parser


def main(args):
    misc.init_distributed_mode(args)

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True

    if not args.dataset in ["cifar10dvs", "ncaltech"]:
        dataset_train = build_dataset(is_train=True, args=args)
        dataset_val = build_dataset(is_train=False, args=args)
    else:
        dataset_train, dataset_val = build_neuromorphic_dataset(args)

    if True:  # args.distributed:
        num_tasks = misc.get_world_size()
        global_rank = misc.get_rank()
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
        )
        print("Sampler_train = %s" % str(sampler_train))
        if args.dist_eval:
            if len(dataset_val) % num_tasks != 0:
                print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                      'This will slightly alter validation results as extra duplicate entries are added to achieve '
                      'equal num of samples per-process.')
            sampler_val = torch.utils.data.DistributedSampler(
                dataset_val, num_replicas=num_tasks, rank=global_rank,
                shuffle=True)  # shuffle=True to reduce monitor bias
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None and not args.eval:
        os.makedirs(args.log_dir, exist_ok=True)
        args.log_dir = os.path.join(args.log_dir,
                                    "{}_{}_{}_{}_{}_{}".format(args.project_name, args.model, args.dataset, args.act_layer, args.mode, args.level))
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = SummaryWriter(log_dir=args.log_dir)
        if args.wandb:
            wandb.init(config=args, project=args.project_name,
                       name="{}_{}_{}_{}_{}_{}".format(args.project_name, args.model, args.dataset, args.act_layer, args.mode, args.level),
                       dir=args.output_dir)
    else:
        log_writer = None

    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, sampler=sampler_train,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=True,
    )

    data_loader_val = torch.utils.data.DataLoader(
        dataset_val, sampler=sampler_val,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=False
    )

    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    if mixup_active:
        print("Mixup is activated!")
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.nb_classes)

    if args.act_layer == "relu":
        activation = nn.ReLU
    elif args.act_layer == "gelu":
        activation = nn.GELU
    else:
        raise NotImplementedError

    if args.act_layer_teacher == "relu":
        activation_teacher = nn.ReLU
    elif args.act_layer_teacher == "gelu":
        activation_teacher = nn.GELU
    else:
        raise NotImplementedError

    if "vit_small" in args.model:
        if "dvs" in args.model:
            assert args.dataset in ["cifar10dvs", "ncaltech"]
            model = models_vit.__dict__[args.model](
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=False,
                act_layer=activation,
                in_channels_dvs=args.in_channels,
                mean=args.mean,
                std=args.std,
            )
        else:
            model = models_vit.__dict__[args.model](
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=False,
                act_layer=activation,
            )
    else:
        model = models_vit.__dict__[args.model](
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            global_pool=args.global_pool,
            act_layer=activation,
        )

    if "dvs" in args.model_teacher:
        assert args.dataset in ["cifar10dvs", "ncaltech"]
        model_teacher = models_vit.__dict__[args.model_teacher](
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            global_pool=False if "vit_small" in args.model else args.global_pool,
            act_layer=activation,
            in_channels_dvs=args.in_channels,
            mean=args.mean,
            std=args.std,
        )
    else:
        model_teacher = models_vit.__dict__[args.model_teacher](
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            global_pool=False if "vit_small" in args.model else args.global_pool,
            act_layer=activation,
        )

    # model_teacher = models_vit.__dict__[args.model_teacher](
    #     num_classes=args.nb_classes,
    #     drop_path_rate=args.drop_path,
    #     global_pool=False if "vit_small" in args.model else args.global_pool,
    #     act_layer=activation_teacher,
    # )

    model_teacher.eval()

    assert args.pretrain_teacher is not None
    print("Load pre-trained teacher checkpoint from: %s" % args.pretrain_teacher)
    checkpoint_teacher = torch.load(args.pretrain_teacher, map_location='cpu')
    checkpoint_model_teacher = checkpoint_teacher if ".bin" in args.pretrain_teacher else checkpoint_teacher['model']
    state_dict_teacher = model_teacher.state_dict()
    for k in ['head.weight', 'head.bias']:
        if k in checkpoint_model_teacher and checkpoint_model_teacher[k].shape != state_dict_teacher[k].shape:
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint_model_teacher[k]

    # interpolate position embedding
    interpolate_pos_embed(model_teacher, checkpoint_model_teacher)

    # load pre-trained model
    msg_teacher = model_teacher.load_state_dict(checkpoint_model_teacher, strict=True)
    print(msg_teacher)

    if args.finetune and not args.eval and not (args.mode == "SNN") and not (args.mode == "QANN-QAT" and args.eval):
        checkpoint = torch.load(args.finetune, map_location='cpu')

        print("Load pre-trained checkpoint from: %s !!!!!!!" % args.finetune)
        checkpoint_model = checkpoint if ".bin" in args.finetune else checkpoint['model']
        state_dict = model.state_dict()
        for k in ['head.weight', 'head.bias']:
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                print(f"Removing key {k} from pretrained checkpoint")
                del checkpoint_model[k]

        # interpolate position embedding
        interpolate_pos_embed(model, checkpoint_model)

        # load pre-trained model
        msg = model.load_state_dict(checkpoint_model, strict=True)
        print(msg)

        # if args.global_pool:
        #     assert set(msg.missing_keys) == {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
        # else:
        #     assert set(msg.missing_keys) == {'head.weight', 'head.bias'}

        # manually initialize fc layer
        if not args.mode == "QANN-QAT":
            trunc_normal_(model.head.weight, std=2e-5)

    if args.rank == 0:
        print("======================== ANN model ========================")
        f = open(f"{args.log_dir}/ann_model_arch.txt", "w+")
        f.write(str(model))
        f.close()
    if args.mode.count("QANN") > 0:
        myquan_replace(model, args.level)
        if args.rank == 0:
            print("======================== QANN model =======================")
            f = open(f"{args.log_dir}/qann_model_arch.txt", "w+")
            f.write(str(model))
            f.close()
    elif args.mode == "SNN":
        myquan_replace(model, args.level)
        checkpoint = torch.load(args.finetune, map_location='cpu') if not args.eval else torch.load(args.resume,
                                                                                                    map_location='cpu')
        print("Load pre-trained checkpoint from: %s" % args.finetune)
        checkpoint_model = checkpoint['model']
        state_dict = model.state_dict()
        for k in ['head.weight', 'head.bias']:
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                print(f"Removing key {k} from pretrained checkpoint")
                del checkpoint_model[k]

        # interpolate position embedding
        interpolate_pos_embed(model, checkpoint_model)

        # load pre-trained model
        msg = model.load_state_dict(checkpoint_model, strict=True)
        print(msg)
        if args.rank == 0:
            print("======================== QANN model =======================")
            f = open(f"{args.log_dir}/qann_model_arch.txt", "w+")
            f.write(str(model))
            f.close()

        # if args.global_pool:
        #     assert set(msg.missing_keys) == {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
        # else:
        #     assert set(msg.missing_keys) == {'head.weight', 'head.bias'}

        # manually initialize fc layer
        # trunc_normal_(model.head.weight, std=2e-5)
        model = SNNWrapper(ann_model=model, cfg=None, time_step=args.time_step, Encoding_type=args.encoding_type,
                           level=args.level, neuron_type=args.neuron_type, model_name=args.model)
        if args.rank == 0:
            print("======================== SNN model =======================")
            f = open(f"{args.log_dir}/snn_model_arch.txt", "w+")
            f.write(str(model))
            f.close()

    model.to(device)
    model_teacher.to(device)

    model_without_ddp = model if args.mode != "SNN" else model.model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("Model = %s" % str(model_without_ddp))
    print('number of params (M): %.2f' % (n_parameters / 1.e6))

    eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

    if args.lr is None:  # only base_lr is specified
        args.lr = args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.accum_iter)
    print("effective batch size: %d" % eff_batch_size)

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_teacher = torch.nn.parallel.DistributedDataParallel(model_teacher, device_ids=[args.gpu])
        model_without_ddp = model.module if args.mode != "SNN" else model.module.model

    # build optimizer with layer-wise lr decay (lrd)
    param_groups = lrd.param_groups_lrd(model_without_ddp, args.weight_decay,
                                        no_weight_decay_list=model_without_ddp.no_weight_decay(),
                                        layer_decay=args.layer_decay
                                        )
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
    loss_scaler = NativeScaler()

    if mixup_fn is not None:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = torch.nn.CrossEntropyLoss()

    print("criterion = %s" % str(criterion))

    if args.mode != "SNN":
        misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)

    if args.eval:
        test_stats = evaluate(data_loader_val, model, device, args)
        print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
        exit(0)

    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()
    max_accuracy = 0.0

    test_stats = evaluate(data_loader_val, model_teacher, device, args)
    print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")

    test_stats = evaluate(data_loader_val, model, device, args)
    print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)
        train_stats = train_one_epoch_distill(
            model, model_teacher, criterion, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            args.clip_grad, mixup_fn,
            log_writer=log_writer,
            args=args
        )
        if args.output_dir:
            misc.save_model(
                args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                loss_scaler=loss_scaler, epoch=epoch)

        test_stats = evaluate(data_loader_val, model, device, args)
        print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
        max_accuracy = max(max_accuracy, test_stats["acc1"])
        print(f'Max accuracy: {max_accuracy:.2f}%')

        if log_writer is not None:
            log_writer.add_scalar('perf/test_acc1', test_stats['acc1'], epoch)
            log_writer.add_scalar('perf/test_acc5', test_stats['acc5'], epoch)
            log_writer.add_scalar('perf/test_loss', test_stats['loss'], epoch)
            if args.wandb:
                epoch_1000x = int(((len(data_loader_train) - 1) / len(data_loader_train) + epoch) * 1000)
                wandb.log({'test_acc1_curve': test_stats['acc1']}, step=epoch_1000x)
                wandb.log({'test_acc5_curve': test_stats['acc5']}, step=epoch_1000x)
                wandb.log({'test_loss_curve': test_stats['loss']}, step=epoch_1000x)
                if args.mode == "SNN":
                    for t in range(model.max_T):
                        wandb.log({'acc1@{}_curve'.format(t + 1): test_stats['acc@{}'.format(t + 1)]}, step=epoch_1000x)

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     **{f'test_{k}': v for k, v in test_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}

        if args.output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        args.output_dir = os.path.join(args.output_dir,
                                       "{}_{}_{}_{}_{}_{}".format(args.project_name, args.model, args.dataset, args.act_layer, args.mode, args.level))
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        print(args.output_dir)
    main(args)
