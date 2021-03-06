# -*- coding: utf-8 -*-

from torchvision import datasets, transforms

from .cifar import *
from .imagenet import *
from .sampler import *

__all__ = ["build_dataset", "IterationSampler"]

_dataset_zoo = {
    "CIFAR100Sub": CIFAR100Sub,
    "ImageNetST": ImageNetDatasetST,
}
_dataset_zoo.update({k: v for k, v in vars(datasets).items()
                     if not k.startswith("__")})

_normalize_rgb = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                      std=[0.229, 0.224, 0.225])
_train_transforms = {
    "CIFAR10": transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        _normalize_rgb,
    ]),
    "ImageNetST": transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ColorJitter(0.2, 0.2, 0.2, 0.1),
        transforms.ToTensor(),
        _normalize_rgb,
    ]),
}
_eval_transforms = {
    "CIFAR10": transforms.Compose([
        transforms.ToTensor(),
        _normalize_rgb,
    ]),
    "ImageNetST": transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        _normalize_rgb,
    ]),
}


def build_dataset(name, *args, eval_only=False, **kwargs):
    if eval_only:
        eval_trans = _eval_transforms[name]
        eval_set = _dataset_zoo[name](*args, train=False, transform=eval_trans, **kwargs)
        return eval_set
    else:
        train_trans = _train_transforms[name]
        eval_trans = _eval_transforms[name]
        train_set = _dataset_zoo[name](*args, train=True, transform=train_trans, **kwargs)
        eval_set = _dataset_zoo[name](*args, train=False, transform=eval_trans, **kwargs)
        return train_set, eval_set
