import os
from pprint import pprint

import torch
import torch.optim as optim
from torch import nn

import passport_generator
from dataset import prepare_dataset, prepare_wm
from experiments.base import Experiment
from experiments.attacks import PGD_L2, Attacker
from experiments.trainer import Trainer
from experiments.trainer_private import TrainerPrivate, TesterPrivate
from experiments.utils import construct_passport_kwargs
from models.alexnet_normal import AlexNetNormal
from models.alexnet_passport_private import AlexNetPassportPrivate
from models.layers.conv2d import ConvBlock
from models.resnet_normal import ResNet18
from models.resnet_passport_private import ResNet18Private


class ClassificationPrivateExperiment(Experiment):
    def __init__(self, args):
        super().__init__(args)

        self.in_channels = 1 if self.dataset == 'mnist' else 3
        self.num_classes = {
            'cifar10': 10,
            'cifar100': 100,
            'caltech-101': 101,
            'caltech-256': 256,
            'imagenet1000': 1000
        }[self.dataset]

        self.train_data, self.valid_data = prepare_dataset(self.args)
        self.wm_data = None

        if self.use_trigger_as_passport:
            self.passport_data = prepare_wm('data/trigger_set/pics', crop=self.imgcrop)
        else:
            self.passport_data = self.valid_data

        if self.train_backdoor:
            self.wm_data = prepare_wm('data/trigger_set/pics', crop=self.imgcrop)

        self.construct_model()
        print("Attacker is PGD")
        self.attacker = PGD_L2(steps=10, device='cuda', max_norm=0.25)

        optimizer = optim.SGD(self.model.parameters(),
                              lr=self.lr,
                              momentum=0.9,
                              weight_decay=0.0001)

        if len(self.lr_config[self.lr_config['type']]) != 0:  # if no specify steps, then scheduler = None
            scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                                       self.lr_config[self.lr_config['type']],
                                                       self.lr_config['gamma'])
        else:
            scheduler = None

        self.trainer = TrainerPrivate(self.model, optimizer, scheduler, self.device)

        if self.is_tl:
            self.finetune_load()
        else:
            self.makedirs_or_load()

    def construct_model(self):
        print('Construct Model')

        def setup_keys():
            if self.key_type != 'random':
                pretrained_from_torch = self.pretrained_path is None
                if self.arch == 'alexnet':
                    norm_type = 'none' if pretrained_from_torch else self.norm_type
                    pretrained_model = AlexNetNormal(self.in_channels,
                                                     self.num_classes,
                                                     norm_type=norm_type,
                                                     pretrained=pretrained_from_torch)
                else:
                    norm_type = 'bn' if pretrained_from_torch else self.norm_type
                    pretrained_model = ResNet18(num_classes=self.num_classes,
                                                norm_type=norm_type,
                                                pretrained=pretrained_from_torch)

                if not pretrained_from_torch:
                    print('Loading pretrained from self-trained model')
                    pretrained_model.load_state_dict(torch.load(self.pretrained_path))
                else:
                    print('Loading pretrained from torch-pretrained model')

                pretrained_model = pretrained_model.to(self.device)
                self.setup_keys(pretrained_model)

        passport_kwargs = construct_passport_kwargs(self)
        self.passport_kwargs = passport_kwargs

        print('Loading arch: ' + self.arch)
        if self.arch == 'alexnet':
            model = AlexNetPassportPrivate(self.in_channels, self.num_classes, passport_kwargs)
        else:
            model = ResNet18Private(num_classes=self.num_classes, passport_kwargs=passport_kwargs)

        self.model = model.to(self.device)

        setup_keys()

        pprint(self.model)

    def setup_keys(self, pretrained_model):
        if self.key_type != 'random':
            n = 1 if self.key_type == 'image' else 20  # any number

            key_x, x_inds = passport_generator.get_key(self.passport_data, n)
            key_x = key_x.to(self.device)
            key_y, y_inds = passport_generator.get_key(self.passport_data, n)
            key_y = key_y.to(self.device)

            passport_generator.set_key(pretrained_model, self.model,
                                       key_x, key_y)

    def training(self):
        best_acc = float('-inf')

        history_file = os.path.join(self.logdir, 'history.csv')
        first = True

        if self.save_interval > 0:
            self.save_model('epoch-0.pth')

        print('Start Training')

        for ep in range(1, self.epochs + 1):
            train_metrics = self.trainer.train(ep, self.train_data, self.attacker, self.wm_data)
            print(f'Sign Detection Accuracy: {train_metrics["sign_acc"] * 100:6.4f}')

            valid_metrics = self.trainer.test(self.valid_data, msg='Testing Result')

            wm_metrics = {}
            if self.train_backdoor:
                wm_metrics = self.trainer.test(self.wm_data, msg='WM Result')

            metrics = {}
            for key in train_metrics: metrics[f'train_{key}'] = train_metrics[key]
            for key in valid_metrics: metrics[f'valid_{key}'] = valid_metrics[key]
            for key in wm_metrics: metrics[f'wm_{key}'] = wm_metrics[key]
            self.append_history(history_file, metrics, first)
            first = False

            if self.save_interval and ep % self.save_interval == 0:
                self.save_model(f'epoch-{ep}.pth')

            if best_acc < metrics['valid_total_acc']:
                print(f'Found best at epoch {ep}\n')
                best_acc = metrics['valid_total_acc']
                self.save_model('best.pth')

            self.save_last_model()

    def evaluate(self):
        self.trainer.test(self.valid_data, self.attacker)

    def transfer_learning(self):
        if not self.is_tl:
            raise Exception('Please run with --transfer-learning')

        is_imagenet = self.num_classes == 1000

        self.num_classes = {
            'cifar10': 10,
            'cifar100': 100,
            'caltech-101': 101,
            'caltech-256': 256,
            'imagenet1000': 1000
        }[self.tl_dataset]

        ##### load clone model #####
        print('Loading clone model')
        if self.arch == 'alexnet':
            tl_model = AlexNetNormal(self.in_channels,
                                     self.num_classes,
                                     self.norm_type,
                                     imagenet=is_imagenet)
        else:
            tl_model = ResNet18(num_classes=self.num_classes,
                                norm_type=self.norm_type,
                                imagenet=is_imagenet)

        ##### load / reset weights of passport layers for clone model #####
        try:
            tl_model.load_state_dict(self.model.state_dict())
        except:
            print('Having problem to direct load state dict, loading it manually')
            if self.arch == 'alexnet':
                for tl_m, self_m in zip(tl_model.features, self.model.features):
                    try:
                        tl_m.load_state_dict(self_m.state_dict())
                    except:
                        print(
                            'Having problem to load state dict usually caused by missing keys, load by strict=False')
                        tl_m.load_state_dict(self_m.state_dict(), False)  # load conv weight, bn running mean
                        tl_m.bn.weight.data.copy_(self_m.get_scale().detach().view(-1))
                        tl_m.bn.bias.data.copy_(self_m.get_bias().detach().view(-1))

            else:
                passport_settings = self.passport_config
                for l_key in passport_settings:  # layer
                    if isinstance(passport_settings[l_key], dict):
                        for i in passport_settings[l_key]:  # sequential
                            for m_key in passport_settings[l_key][i]:  # convblock
                                tl_m = tl_model.__getattr__(l_key)[int(i)].__getattr__(m_key)  # type: ConvBlock
                                self_m = self.model.__getattr__(l_key)[int(i)].__getattr__(m_key)

                                try:
                                    tl_m.load_state_dict(self_m.state_dict())
                                except:
                                    print(f'{l_key}.{i}.{m_key} cannot load state dict directly')
                                    tl_m.load_state_dict(self_m.state_dict(), False)
                                    tl_m.bn.weight.data.copy_(self_m.get_scale().detach().view(-1))
                                    tl_m.bn.bias.data.copy_(self_m.get_bias().detach().view(-1))

                    else:
                        tl_m = tl_model.__getattr__(l_key)
                        self_m = self.model.__getattr__(l_key)

                        try:
                            tl_m.load_state_dict(self_m.state_dict())
                        except:
                            print(f'{l_key} cannot load state dict directly')
                            tl_m.load_state_dict(self_m.state_dict(), False)
                            tl_m.bn.weight.data.copy_(self_m.get_scale().detach().view(-1))
                            tl_m.bn.bias.data.copy_(self_m.get_bias().detach().view(-1))

        tl_model.to(self.device)
        print('Loaded clone model')

        # tl scheme setup
        if self.tl_scheme == 'rtal':
            # rtal = reset last layer + train all layer
            # ftal = train all layer
            try:
                if isinstance(tl_model.classifier, nn.Sequential):
                    tl_model.classifier[-1].reset_parameters()
                else:
                    tl_model.classifier.reset_parameters()
            except:
                tl_model.linear.reset_parameters()

        optimizer = optim.SGD(tl_model.parameters(),
                              lr=self.lr,
                              momentum=0.9,
                              weight_decay=0.0005)

        if len(self.lr_config[self.lr_config['type']]) != 0:  # if no specify steps, then scheduler = None
            scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                                       self.lr_config[self.lr_config['type']],
                                                       self.lr_config['gamma'])
        else:
            scheduler = None

        tl_trainer = Trainer(tl_model,
                             optimizer,
                             scheduler,
                             self.device)
        tester = TesterPrivate(self.model,
                               self.device)

        history_file = os.path.join(self.logdir, 'history.csv')
        first = True
        best_acc = 0

        for ep in range(1, self.epochs + 1):
            train_metrics = tl_trainer.train(ep, self.train_data)
            valid_metrics = tl_trainer.test(self.valid_data)

            ##### load transfer learning weights from clone model  #####
            try:
                self.model.load_state_dict(tl_model.state_dict())
            except:
                if self.arch == 'alexnet':
                    for tl_m, self_m in zip(tl_model.features, self.model.features):
                        try:
                            self_m.load_state_dict(tl_m.state_dict())
                        except:
                            self_m.load_state_dict(tl_m.state_dict(), False)
                else:
                    passport_settings = self.passport_config
                    for l_key in passport_settings:  # layer
                        if isinstance(passport_settings[l_key], dict):
                            for i in passport_settings[l_key]:  # sequential
                                for m_key in passport_settings[l_key][i]:  # convblock
                                    tl_m = tl_model.__getattr__(l_key)[int(i)].__getattr__(m_key)
                                    self_m = self.model.__getattr__(l_key)[int(i)].__getattr__(m_key)

                                    try:
                                        self_m.load_state_dict(tl_m.state_dict())
                                    except:
                                        self_m.load_state_dict(tl_m.state_dict(), False)
                        else:
                            tl_m = tl_model.__getattr__(l_key)
                            self_m = self.model.__getattr__(l_key)

                            try:
                                self_m.load_state_dict(tl_m.state_dict())
                            except:
                                self_m.load_state_dict(tl_m.state_dict(), False)

            wm_metrics = tester.test_signature()

            if self.train_backdoor:
                backdoor_metrics = tester.test(self.wm_data, 'Old WM Accuracy')

            metrics = {}
            for key in train_metrics: metrics[f'train_{key}'] = train_metrics[key]
            for key in valid_metrics: metrics[f'valid_{key}'] = valid_metrics[key]
            for key in wm_metrics: metrics[f'old_wm_{key}'] = wm_metrics[key]
            if self.train_backdoor:
                for key in backdoor_metrics: metrics[f'backdoor_{key}'] = backdoor_metrics[key]
            self.append_history(history_file, metrics, first)
            first = False

            if self.save_interval and ep % self.save_interval == 0:
                self.save_model(f'epoch-{ep}.pth')
                self.save_model(f'tl-epoch-{ep}.pth', tl_model)

            if best_acc < metrics['valid_acc']:
                print(f'Found best at epoch {ep}\n')
                best_acc = metrics['valid_acc']
                self.save_model('best.pth')
                self.save_model('tl-best.pth', tl_model)

            self.save_last_model()
