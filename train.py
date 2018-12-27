#!/usr/bin/env python
# encoding: utf-8
'''
@author: wujiyang
@contact: wujiyang@hust.edu.cn
@file: train.py.py
@time: 2018/12/21 17:37
@desc: train script for deep face recognition
'''

import os
import torch.utils.data
from torch import nn
from torch.nn import DataParallel
from datetime import datetime
from backbone.mobilefacenet import MobileFaceNet
from backbone.resnet import ResNet50, ResNet101
from backbone.arcfacenet import SEResNet_IR
from margin.ArcMarginProduct import ArcMarginProduct
from utils.logging import init_log
from dataset.casia_webface import CASIAWebFace
from dataset.lfw import LFW
from dataset.agedb import AgeDB30
from dataset.cfp import CFP_FP
from torch.optim import lr_scheduler
import torch.optim as optim
import time
from eval_lfw import evaluation_10_fold, getFeatureFromTorch
import numpy as np
import torchvision.transforms as transforms
import argparse

def train(args):
    # gpu init
    multi_gpus = False
    if len(args.gpus.split(',')) > 1:
        multi_gpus = True
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # log init
    start_epoch = 1
    save_dir = os.path.join(args.save_dir, args.model_pre + args.backbone.upper() + '_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    if os.path.exists(save_dir):
        raise NameError('model dir exists!')
    os.makedirs(save_dir)
    logging = init_log(save_dir)
    _print = logging.info

    # dataset loader
    transform = transforms.Compose([
        transforms.ToTensor(),  # range [0, 255] -> [0.0,1.0]
        transforms.Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5))  # range [0.0, 1.0] -> [-1.0,1.0]
    ])
    # train dataset
    trainset = CASIAWebFace(args.train_root, args.train_file_list, transform=transform)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size,
                                              shuffle=True, num_workers=8, drop_last=False)
    # test dataset
    lfwdataset = LFW(args.lfw_test_root, args.lfw_file_list, transform=transform)
    lfwloader = torch.utils.data.DataLoader(lfwdataset, batch_size=128,
                                             shuffle=False, num_workers=4, drop_last=False)
    agedbdataset = AgeDB30(args.agedb_test_root, args.agedb_file_list, transform=transform)
    agedbloader = torch.utils.data.DataLoader(agedbdataset, batch_size=128,
                                            shuffle=False, num_workers=4, drop_last=False)
    cfpfpdataset = CFP_FP(args.cfpfp_test_root, args.cfpfp_file_list, transform=transform)
    cfpfploader = torch.utils.data.DataLoader(cfpfpdataset, batch_size=128,
                                              shuffle=False, num_workers=4, drop_last=False)

    # define backbone and margin layer
    if args.backbone == 'MobileFace':
        net = MobileFaceNet()
    elif args.backbone is 'Res50':
        net = ResNet50()
    elif args.backbone == 'Res101':
        net = ResNet101()
    elif args.backbone == 'Res50-IR':
        net = SEResNet_IR(50, feature_dim=args.feature_dim, mode='ir')
    elif args.backbone == 'SERes50-IR':
        net = SEResNet_IR(50, feature_dim=args.feature_dim, mode='se_ir')
    else:
        print(args.backbone, ' is not available!')

    if args.margin_type == 'arcface':
        margin = ArcMarginProduct(args.feature_dim, trainset.class_nums)
    elif args.margin_type == 'cosface':
        pass
    elif args.margin_type == 'sphereface':
        pass
    else:
        print(args.margin_type, 'is not available!')

    # TODO: Adaptive the finetune process for different backbone strcuture
    if args.pretrain:
        print('load pretrained model from:', args.pretrain)
        # load pretrained model
        net_old = MobileFaceNet()
        net_old.load_state_dict(torch.load(args.pretrain)['net_state_dict'])
        # filter the parameters not in new model
        net_dict = net.state_dict()
        net_old = {k: v for k, v in net_old.state_dict().items() if k in net_dict}
        # update the new state_dict
        net_dict.update(net_old)
        net.load_state_dict(net_dict)

    if args.resume:
        print('resume the model parameters from: ', args.resume)
        ckpt = torch.load(args.resume)
        net.load_state_dict(ckpt['net_state_dict'])
        start_epoch = ckpt['epoch'] + 1

    # define optimizers for different layer
    ignored_params_id = []
    ignored_params_id += list(map(id, margin.weight))
    prelu_params = []
    for m in net.modules():
        if isinstance(m, nn.PReLU):
            ignored_params_id += list(map(id, m.parameters()))
            prelu_params += m.parameters()
    base_params = filter(lambda p: id(p) not in ignored_params_id, net.parameters())

    optimizer_ft = optim.SGD([
        {'params': base_params, 'weight_decay': 5e-5},
        {'params': margin.weight, 'weight_decay': 5e-4},
        {'params': prelu_params, 'weight_decay': 0.0}
    ], lr=0.1, momentum=0.9, nesterov=True)

    exp_lr_scheduler = lr_scheduler.MultiStepLR(optimizer_ft, milestones=[20, 35, 45], gamma=0.1)

    if multi_gpus:
        net = DataParallel(net).to(device)
        margin = DataParallel(margin).to(device)
    else:
        net = net.to(device)
        margin = margin.to(device)
    criterion = torch.nn.CrossEntropyLoss().to(device)

    best_lfw_acc = 0.0
    best_lfw_epoch = 0
    best_agedb30_acc = 0.0
    best_agedb30_epoch = 0
    best_cfp_fp_acc = 0.0
    best_cfp_fp_epoch = 0

    for epoch in range(start_epoch, args.total_epoch + 1):
        exp_lr_scheduler.step()
        # train model
        _print('Train Epoch: {}/{} ...'.format(epoch, args.total_epoch))
        net.train()

        since = time.time()
        iters = 0
        for data in trainloader:
            img, label = data[0].to(device), data[1].to(device)
            batch_size = img.size(0)
            optimizer_ft.zero_grad()

            raw_logits = net(img)
            output = margin(raw_logits, label)
            total_loss = criterion(output, label)
            total_loss.backward()
            optimizer_ft.step()

            # print train information
            iters = iters + 1
            if iters % 100 == 0:
                time_cur = (time.time() - since) / 100
                since = time.time()
                print("Iters: {:4d}, loss: {:.4f}, time: {:.4f} s/iter, learning rate: {}".format(iters, total_loss.item(), time_cur, exp_lr_scheduler.get_lr()[0]))

        # save model
        if epoch % args.save_freq == 0:
            msg = 'Saving checkpoint: {}'.format(epoch)
            _print(msg)
            if multi_gpus:
                net_state_dict = net.module.state_dict()
            else:
                net_state_dict = net.state_dict()
            if not os.path.exists(save_dir):
                os.mkdir(save_dir)
            torch.save({
                'epoch': epoch,
                'net_state_dict': net_state_dict},
                os.path.join(save_dir, '%03d.ckpt' % epoch))

        if epoch % args.test_freq == 0:
            # test model on lfw
            getFeatureFromTorch('./result/cur_epoch_lfw_result.mat', net, device, lfwdataset, lfwloader)
            accs = evaluation_10_fold('./result/cur_epoch_lfw_result.mat')
            _print('LFW Ave Accuracy: {:.4f}'.format(np.mean(accs) * 100))
            if best_lfw_acc < np.mean(accs) * 100:
                best_lfw_acc = np.mean(accs) * 100
                best_lfw_epoch = epoch

            # test model on AgeDB30
            getFeatureFromTorch('./result/cur_epoch_agedb30_result.mat', net, device, agedbdataset, agedbloader)
            accs = evaluation_10_fold('./result/cur_epoch_agedb30_result.mat')
            _print('AgeDB-30 Ave Accuracy: {:.4f}'.format(np.mean(accs) * 100))
            if best_agedb30_acc < np.mean(accs) * 100:
                best_agedb30_acc = np.mean(accs) * 100
                best_agedb30_epoch = epoch

            # test model on CFP-FP
            getFeatureFromTorch('./result/cur_epoch_cfpfp_result.mat', net, device, cfpfpdataset, cfpfploader)
            accs = evaluation_10_fold('./result/cur_epoch_cfpfp_result.mat')
            _print('CFP-FP Ave Accuracy: {:.4f}'.format(np.mean(accs) * 100))
            if best_cfp_fp_acc < np.mean(accs) * 100:
                best_cfp_fp_acc = np.mean(accs) * 100
                best_cfp_fp_epoch = epoch
            _print('Current Best Accuracy: LFW: {:.4f} in Epoch: {}, AgeDB-30: {:.4f} in Epoch: {} and CFP-FP: {:.4f} in Epoch {}'.format(
                best_lfw_acc, best_lfw_epoch, best_agedb30_acc, best_agedb30_epoch, best_cfp_fp_acc, best_cfp_fp_epoch))

    _print('Finally Best Accuracy: LFW: {:.4f} in Epoch: {}, AgeDB-30: {:.4f} in Epoch: {} and CFP-FP: {:.4f} in Epoch {}'.format(
        best_lfw_acc, best_lfw_epoch, best_agedb30_acc, best_agedb30_epoch, best_cfp_fp_acc, best_cfp_fp_epoch))
    print('finishing training')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch for deep face recognition')
    parser.add_argument('--train_root', type=str, default='/media/ramdisk/webface_align_112/', help='train image root')
    parser.add_argument('--train_file_list', type=str, default='/media/ramdisk/webface_align_train.list', help='train list')
    parser.add_argument('--lfw_test_root', type=str, default='/media/ramdisk/lfw_align_112', help='lfw image root')
    parser.add_argument('--lfw_file_list', type=str, default='/media/ramdisk/pairs.txt', help='lfw pair file list')
    parser.add_argument('--agedb_test_root', type=str, default='/media/sda/AgeDB-30/agedb30_align_112', help='agedb image root')
    parser.add_argument('--agedb_file_list', type=str, default='/media/sda/AgeDB-30/agedb_30_pair.txt', help='agedb pair file list')
    parser.add_argument('--cfpfp_test_root', type=str, default='/media/sda/CFP-FP/CFP_FP_aligned_112', help='agedb image root')
    parser.add_argument('--cfpfp_file_list', type=str, default='/media/sda/CFP-FP/cfp_fp_pair.txt', help='agedb pair file list')

    parser.add_argument('--backbone', type=str, default='MobileFace', help='MobileFace, Res50, Res101, Res50-IR, SERes50-IR, SphereNet')
    parser.add_argument('--margin_type', type=str, default='arcface', help='arcface, cosface, sphereface')
    parser.add_argument('--feature_dim', type=int, default=128, help='feature dimension, 128 or 512')
    parser.add_argument('--batch_size', type=int, default=256, help='batch size')
    parser.add_argument('--total_epoch', type=int, default=50, help='total epochs')

    parser.add_argument('--save_freq', type=int, default=1, help='save frequency')
    parser.add_argument('--test_freq', type=int, default=1, help='test frequency')
    parser.add_argument('--resume', type=str, default='', help='resume model')
    parser.add_argument('--pretrain', type=str, default='', help='pretrain model')
    parser.add_argument('--save_dir', type=str, default='./model', help='model save dir')
    parser.add_argument('--model_pre', type=str, default='CASIA_', help='model prefix')
    parser.add_argument('--gpus', type=str, default='0,1', help='model prefix')

    args = parser.parse_args()

    train(args)


