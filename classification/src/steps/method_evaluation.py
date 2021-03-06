import os

import numpy as np

import torch
import torch.optim
import torch.utils.data.sampler

from utils import backbones
from classification.src.loaders.data_managers import SetDataManager
from classification.src.methods import *
from utils.io_utils import (
    model_dict,
    path_to_step_output,
    set_and_print_random_seed,
    get_path_to_json
)
from utils.utils import random_swap_numpy


class MethodEvaluation():
    """
    This step handles the evaluation of the trained model on the novel dataset
    """
    def __init__(
            self,
            dataset,
            backbone='Conv4',
            method='baseline',
            train_n_way=5,
            test_n_way=5,
            n_shot=5,
            n_query=15,
            train_aug=False,
            split='novel',
            save_iter=-1,
            n_iter=600,
            adaptation=False,
            random_seed=None,
            n_swaps=0,
    ):
        """
        Args:
            dataset (str): CUB/miniImageNet
            model (str): Conv{4|6} / ResNet{10|18|34|50|101}
            method (str): baseline/baseline++/protonet/matchingnet/relationnet{_softmax}/maml{_approx}
            train_n_way (int): number of labels in a classification task during training
            test_n_way (int): number of labels in a classification task during testing
            n_shot (int): number of labeled data in each class in a classification task
            n_query (int): number of query data for each class in a classification task
            train_aug (bool): perform data augmentation or not during training
            split (str): which dataset is considered (base, val or novel)
            save_iter (int): save feature from the model trained in x epoch, use the best model if x is -1
            n_iter (int): number of classification tasks on which the model is tested
            adaptation (boolean): further adaptation in test time or not
            random_seed (int): seed for random instantiations ; if none is provided, a seed is randomly defined
            n_swaps (int): number of swaps between labels in the support set of each classification task, in order to
            test the robustness to label noise

        """

        self.dataset = dataset
        self.backbone = backbone
        self.method = method
        self.train_n_way = train_n_way
        self.test_n_way = test_n_way
        self.n_shot = n_shot
        self.n_query = n_query
        self.train_aug = train_aug
        self.split = split
        self.save_iter = save_iter
        self.n_iter = n_iter
        self.adaptation = adaptation
        self.random_seed = random_seed
        self.n_swaps = n_swaps

        self.checkpoint_dir = path_to_step_output(
            self.dataset,
            self.backbone,
            self.method,
        )

    def apply(self, model_state, features_and_labels=None):
        """
        Executes MethodEvaluation step
        Args:
            model_state (dict): contains the state of the trained model and the number of training epochs
            features_and_labels (Tuple[numpy.array, numpy.array]): contains the features and labels of all images in
            the evaluation dataset

        Returns:
            float: average accuracy on few shot classification tasks sampled from the evaluation dataset
        """
        set_and_print_random_seed(self.random_seed, True, self.checkpoint_dir)

        acc_all = []

        model = self._load_model(model_state)

        split = self.split
        if self.save_iter != -1:
            split_str = split + "_" + str(self.save_iter)
        else:
            split_str = split

        if self.method in ['maml', 'maml_approx']:  # maml do not support testing with feature
            if 'Conv' in self.backbone:
                image_size = 84
            else:
                image_size = 224

            set_data_manager = SetDataManager(
                image_size,
                n_episode=self.n_iter,
                n_query=self.n_query,
                n_way=self.test_n_way,
                n_support=self.n_shot,
            )

            path_to_data_file = get_path_to_json(self.dataset, self.split)

            novel_loader = set_data_manager.get_data_loader(path_to_data_file, aug=False)
            if self.adaptation:
                model.task_update_num = 100  # We perform adaptation on MAML simply by updating more times.
            model.eval()
            acc_mean, acc_std = model.eval_loop(novel_loader, n_swaps=self.n_swaps, return_std=True)

        else:
            features_per_label = self._process_features(features_and_labels)

            for i in range(self.n_iter):
                acc = self._feature_evaluation(features_per_label, model)
                acc_all.append(acc)
                if i % 10 == 0:
                    print('{}/{}'.format(i, self.n_iter))

            acc_all = np.asarray(acc_all)
            acc_mean = float(np.mean(acc_all))
            acc_std = float(np.std(acc_all))
            print('%d Test Acc = %4.2f%% +- %4.2f%%' % (self.n_iter, acc_mean, self._confidence_interval(acc_std)))
        with open(os.path.join(self.checkpoint_dir, 'results.txt'), 'a') as f:
            aug_str = '-aug' if self.train_aug else ''
            aug_str += '-adapted' if self.adaptation else ''
            if self.method in ['baseline', 'baseline++']:
                exp_setting = '%s-%s-%s-%s%s %sshot %sway_test %sswaps' % (
                    self.dataset, split_str, self.backbone, self.method, aug_str, self.n_shot,
                    self.test_n_way, self.n_swaps)
            else:
                exp_setting = '%s-%s-%s-%s%s %sshot %sway_train %sway_test' % (
                    self.dataset, split_str, self.backbone, self.method, aug_str, self.n_shot, self.train_n_way,
                    self.test_n_way)
            acc_str = '%d Test Acc = %4.2f%% +- %4.2f%%' % (
                self.n_iter, acc_mean, self._confidence_interval(acc_std))
            f.write(
                'Setting: %s\n Retrieved model from epoch %s\n Acc: %s \n' % (
                    exp_setting, model_state['epoch'], acc_str
                )
            )
        return acc_mean

    def dump_output(self, _, output_folder, output_name, **__):
        pass

    def _feature_evaluation(self, features_per_label, model):
        """
        Evaluates the model on one classification task
        Args:
            features_per_label (dict): a dict containing the feature vectors for each label
            model (torch.nn.Module): model to evaluate

        Returns:
            float: accuracy on the classification of query data, in percents

        """
        z_all = self._set_classification_task(features_per_label)

        model.n_query = self.n_query
        if self.adaptation:
            scores = model.set_forward_adaptation(z_all, is_feature=True)
        else:
            scores = model.set_forward(z_all, is_feature=True)
        pred = scores.data.cpu().numpy().argmax(axis=1)
        y = np.repeat(range(self.test_n_way), self.n_query)
        acc = np.mean(pred == y) * 100
        return acc

    def _set_classification_task(self, features_per_label):
        """
        Defines one classification task, which is composed of a support set and a query set.
        Args:
            features_per_label (dict): a dict containing the feature vectors for each label

        Returns:
            torch.Tensor: shape(self.test_n_way, self.n_shot+self.n_query, feature_vector_dim) features vectors for
            support and query, set by class, with self.n_swaps swaps in the label (cf random_swap_numpy)

        """
        class_list = features_per_label.keys()

        select_class = np.random.choice(list(class_list), size=self.test_n_way, replace=False)
        z_all = []
        for cl in select_class:
            img_feat = features_per_label[cl]
            perm_ids = np.random.permutation(len(img_feat)).tolist()
            z_all.append([np.squeeze(img_feat[perm_ids[i]]) for i in range(self.n_shot + self.n_query)])  # stack each batch

        z_all = np.array(z_all)
        z_all = random_swap_numpy(z_all, self.n_swaps, self.n_shot)
        z_all = torch.from_numpy(z_all)

        return z_all

    def _load_model(self, model_state):
        """
        Load model from training
        Args:
            model_state: dict containing the state of the trained model

        Returns:
            torch.nn.Module: model with loaded parameters, ready for evaluation
        """
        few_shot_params = dict(n_way=self.test_n_way, n_support=self.n_shot)

        # Define model
        if self.method == 'baseline':
            model = BaselineFinetune(model_dict[self.backbone], **few_shot_params)
        elif self.method == 'baseline++':
            model = BaselineFinetune(model_dict[self.backbone], loss_type='dist', **few_shot_params)
        elif self.method == 'protonet':
            model = ProtoNet(model_dict[self.backbone], **few_shot_params)
        elif self.method == 'matchingnet':
            model = MatchingNet(model_dict[self.backbone], **few_shot_params)
        elif self.method in ['relationnet', 'relationnet_softmax']:
            if self.backbone == 'Conv4':
                feature_model = backbones.Conv4NP
            elif self.backbone == 'Conv6':
                feature_model = backbones.Conv6NP
            elif self.backbone == 'Conv4S':
                feature_model = backbones.Conv4SNP
            else:
                feature_model = lambda: model_dict[self.backbone](flatten=False)
            loss_type = 'mse' if self.method == 'relationnet' else 'softmax'
            model = RelationNet(feature_model, loss_type=loss_type, **few_shot_params)
        elif self.method in ['maml', 'maml_approx']:
            backbones.ConvBlock.maml = True
            backbones.SimpleBlock.maml = True
            backbones.BottleneckBlock.maml = True
            backbones.ResNet.maml = True
            model = MAML(model_dict[self.backbone], approx=(self.method == 'maml_approx'), **few_shot_params)
        else:
            raise ValueError('Unknown method')

        model = model.cuda()

        # Fetch model parameters
        if not self.method in ['baseline', 'baseline++']:
            model.load_state_dict(model_state['state'])

        model.eval()

        return model

    def _process_features(self, features_and_labels):
        """
        Process features from numpy arrays to a dictionary
        Args:
            features_and_labels (tuple): a tuple (features, labels)

        Returns:
            dict: a dict where keys are the labels and values are the corresponding feature vectors
        """
        features, labels = features_and_labels

        while not features[-1].any():
            features = np.delete(features, -1, axis=0)
            labels = np.delete(labels, -1, axis=0)

        features_per_label = {
            label: []
            for label in np.unique(np.array(labels)).tolist()
        }

        for ind in range(len(labels)):
            features_per_label[labels[ind]].append(features[ind])

        return features_per_label

    def _confidence_interval(self, std):
        """
        Computes statistical confidence interval of the results from standard deviation and number of iterations
        Args:
            std (float): standard deviation

        Returns:
            float: confidence interval
        """
        return 1.96 * std / np.sqrt(self.n_iter)
