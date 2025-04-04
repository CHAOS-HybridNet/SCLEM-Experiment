import os
import random
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import tensor
from torch.utils.data import DataLoader
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import dgl
import dgl.data.utils as U
import time
import pickle
from models.layers import *
from sklearn.ensemble import RandomForestClassifier, AdaBoostClassifier, GradientBoostingClassifier
import copy
from sklearn.metrics import precision_score,f1_score,recall_score
import warnings
warnings.filterwarnings('ignore')

class UnircaDataset():
    """
    参数
    ----------
    dataset_path: str
        数据存放位置。
        举例: 'train_Xs.pkl' （67 * 14 * 40）（图数 * 节点数 * 节点向量维数）
    labels_path: str
        标签存放位置。
        举例: 'train_ys_anomaly_type.pkl' （67）
    topology: str
        图的拓扑结构存放位置
        举例：'topology.pkl'
    aug: boolean (default: False)
        需要数据增强，该值设置为True
    aug_size: int (default: 0)
        数据增强时，每个label对应的样本数
    shuffle: boolean (default: False)
        load()完成以后，若shuffle为True，则打乱self.graphs 和 self.labels （同步）
    """

    def __init__(self, dataset_path, labels_path, topology, aug=False, aug_size=0, shuffle=False):
        self.dataset_path = dataset_path
        self.labels_path = labels_path
        self.topology = topology
        self.aug = aug
        self.aug_size = aug_size
        self.graphs = []
        self.labels = []
        self.load()
        if shuffle:
            self.shuffle()

    def __getitem__(self, idx):
        return self.graphs[idx], self.labels[idx]

    def __len__(self):
        return len(self.graphs)

    def load(self):
        """ __init__()  中使用，作用是装载 self.graphs 和 self.labels，若aug为True，则进行数据增强操作。
        """
        Xs = tensor(U.load_info(self.dataset_path))
        ys = tensor(U.load_info(self.labels_path))
        topology = U.load_info(self.topology)
        assert Xs.shape[0] == ys.shape[0]
        if self.aug:
            Xs, ys = self.aug_data(Xs, ys)

        for X in Xs:
            g = dgl.graph(topology)  # 同质图
            # 若有0入度节点，给这些节点加自环
            in_degrees = g.in_degrees()
            zero_indegree_nodes = [i for i in range(len(in_degrees)) if in_degrees[i].item() == 0]
            for node in zero_indegree_nodes:
                g.add_edges(node, node)

            g.ndata['attr'] = X
            self.graphs.append(g)
        self.labels = ys

    def shuffle(self):
        graphs_labels = [(g, l) for g, l in zip(self.graphs, self.labels)]
        random.shuffle(graphs_labels)
        self.graphs = [i[0] for i in graphs_labels]
        self.labels = [i[1] for i in graphs_labels]

    def aug_data(self, Xs, ys):
        """ load() 中使用，作用是数据增强
        参数
        ----------
        Xs: tensor
            多个图对应的特征向量矩阵。
            举例：67个图对应的Xs规模为 67 * 14 * 40 （67个图，每个图14个节点）
        ys: tensor
            每个图对应的label，要求是从0开始的整数。
            举例：如果一共有10个label，那么ys中元素值为 0, 1, 2, 3, 4, 5, 6, 7, 8, 9
        self.aug_size: int
            数据增强时，每个label对应的样本数

        返回值
        ----------
        aug_Xs: tensor
            数据增强的结果
        aug_ys: tensor
            数据增强的结果
        """
        aug_Xs = []
        aug_ys = []
        num_label = len(set([y.item() for y in ys]))
        grouped_Xs = [[] for i in range(num_label)]
        for X, y in zip(Xs, ys):
            grouped_Xs[y.item()].append(X)
        for group_idx in range(len(grouped_Xs)):
            cur_Xs = grouped_Xs[group_idx]
            n = len(cur_Xs)
            m = Xs.shape[1]
            while len(cur_Xs) < self.aug_size:
                select = np.random.choice(n, m)
                aug_X = torch.zeros_like(Xs[0])
                for i, j in zip(select, range(m)):
                    aug_X[j] = cur_Xs[i][j].detach().clone()
                cur_Xs.append(aug_X)
            for X in cur_Xs:
                aug_Xs.append(X)
                aug_ys.append(group_idx)
        aug_Xs = torch.stack(aug_Xs, 0)
        aug_ys = tensor(aug_ys)
        return aug_Xs, aug_ys


class RawDataProcess():
    """用来处理原始数据的类
    参数
    ----------
    config: dict
        配置参数
        Xs: 多个图的特征向量矩阵
        data_dir: 数据和结果存放路径
        dataset: 数据集名称 可选['21aiops', 'gaia']
    """

    def __init__(self, config, services, anomalys):
        self.config = config
        self.services = services
        self.anomalys = anomalys

    def process(self):
        """ 用来获取并保存中间数据
        输入：
            sentence_embedding.pkl
            demo.csv
        输出：
            训练集：
                train_Xs.pkl
                train_ys_anomaly_type.pkl
                train_ys_service.pkl
            测试集：
                test_Xs.pkl
                test_ys_anomaly_type.pkl
                test_ys_service.pkl
            拓扑：
                topology.pkl
        """
        run_table = pd.read_csv(os.path.join(self.config['data_dir'], self.config['run_table']), index_col=0)

        
        Xs = U.load_info(os.path.join(self.config['data_dir'], self.config['Xs']))
        Xs = np.array(Xs)
        label_types = ['anomaly_type', 'service']

        label_dict = {label_type: None for label_type in label_types}
        for label_type in label_types:
            label_dict[label_type] = self.get_label(label_type, run_table)

        save_dir = self.config['save_dir']
#         train_size = self.config['train_size']
        train_index = np.where(run_table['data_type'].values=='train')
        test_index = np.where(run_table['data_type'].values=='test')
        train_size = len(train_index[0])

        # 保存特征向量，特征向量是先训练集后测试集
#         print(train_index)
        U.save_info(os.path.join(save_dir, 'train_Xs.pkl'), Xs[: train_size])
        U.save_info(os.path.join(save_dir, 'test_Xs.pkl'), Xs[train_size: ])
        # 保存标签
        for label_type, labels in label_dict.items():
            U.save_info(os.path.join(save_dir, f'train_ys_{label_type}.pkl'), labels[train_index])
            U.save_info(os.path.join(save_dir, f'test_ys_{label_type}.pkl'), labels[test_index])
        # 保存拓扑
        topology = self.get_topology()
        U.save_info(os.path.join(save_dir, 'topology.pkl'), topology)
        # 保存边的类型(异质图)
        if self.config['heterogeneous']:
            edge_types = self.get_edge_types()
            U.save_info(os.path.join(save_dir, 'edge_types.pkl'), edge_types)

    def get_label(self, label_type, run_table):
        """ process() 中调用，用来获取label
        参数
        ----------
        label_type: str
            label的类型，可选：['service', 'anomaly_type']
        run_table: pd.DataFrame

        返回值
        ----------
        labels: torch.tensor()
            label列表
        """
        if label_type == "service":
            meta_labels = sorted(list(set(self.services)))
        elif label_type == "anomaly_type":
            meta_labels = sorted(list(set(self.anomalys)))
        else:
            raise Exception("unknown lable_type")
        # meta_labels = sorted(list(set(list(run_table[label_type]))))
        labels_idx = {label: idx for label, idx in zip(meta_labels, range(len(meta_labels)))}
        labels = np.array(run_table[label_type].apply(lambda label_str: labels_idx[label_str]))
        return labels

    def get_topology(self):
        """ process() 中调用，用来获取topology
        """
        dataset = self.config['dataset']
        if self.config['heterogeneous']:
            # 异质图
            if dataset == 'gaia':
                topology = (
                [8, 6, 8, 4, 6, 4, 2, 9, 1, 3, 3, 7, 1, 7, 5, 0, 8, 8, 9, 9, 8, 8, 9, 9, 8, 8, 9, 9, 2, 2, 3, 3, 0, 0, 1, 1, 4, 4, 5, 5, 2, 2, 3, 3, 6, 7, 6, 7, 4, 5, 4, 5, 2, 3, 2, 3, 0, 1, 0, 1, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7],
                [6, 8, 4, 8, 4, 6, 9, 2, 3, 1, 7, 3, 7, 1, 0, 5, 6, 7, 6, 7, 4, 5, 4, 5, 2, 3, 2, 3, 0, 1, 0, 1, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 8, 8, 9, 9, 8, 8, 9, 9, 8, 8, 9, 9, 2, 2, 3, 3, 0, 0, 1, 1, 4, 4, 5, 5, 2, 2, 3, 3])
            elif dataset == '20aiops':
                topology = (
                [2, 3, 4, 5, 6, 7, 8, 9, 13, 10, 11, 12, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 6,
                 7, 8, 9, 13, 13, 10, 10, 11, 11, 12, 12, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 0,
                 0, 0, 0, 4, 5, 2, 6, 3, 7, 5, 9],
                [2, 3, 4, 5, 6, 7, 8, 9, 13, 10, 11, 12, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 1, 6, 7, 8, 9, 0,
                 0, 0, 0, 4, 5, 2, 6, 3, 7, 5, 9, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 6, 7, 8,
                 9, 13, 13, 10, 10, 11, 11, 12, 12])
            elif dataset == '21aiops':
                topology = (
                    [12, 12, 13, 13, 0, 0, 0, 0, 1, 1, 1, 1, 8, 8, 9, 9, 10, 10, 11, 11, 8, 8, 9, 9, 10, 10, 11, 11, 
                     2, 2, 2, 2, 3, 3, 3, 3, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 0, 1, 8, 
                     9, 10, 11, 2, 3, 14, 15, 16, 17, 0, 1, 0, 1, 8, 9, 10, 11, 8, 9, 10, 11, 6, 4, 6, 4, 6, 4, 6, 4, 
                     2, 3, 2, 3, 2, 3, 2, 3, 14, 15, 16, 17, 14, 15, 16, 17, 7, 7, 7, 7, 5, 5, 5, 5, 2, 2, 2, 2, 3, 3, 
                     3, 3, 0, 1, 8, 9, 10, 11, 2, 3, 14, 15, 16, 17],
                    [0, 1, 0, 1, 8, 9, 10, 11, 8, 9, 10, 11, 6, 4, 6, 4, 6, 4, 6, 4, 2, 3, 2, 3, 2, 3, 2, 3, 14, 15, 16,
                     17, 14, 15, 16, 17, 7, 7, 7, 7, 5, 5, 5, 5, 2, 2, 2, 2, 3, 3, 3, 3, 0, 1, 8, 9, 10, 11, 2, 3, 14, 15,
                     16, 17, 12, 12, 13, 13, 0, 0, 0, 0, 1, 1, 1, 1, 8, 8, 9, 9, 10, 10, 11, 11, 8, 8, 9, 9, 10, 10, 11, 11,
                     2, 2, 2, 2, 3, 3, 3, 3, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 0, 1, 8, 9, 10,
                     11, 2, 3, 14, 15, 16, 17]
                )
            else:
                raise Exception()
        else:
            # 同质图
            if dataset == 'gaia':
                topology = (
                    [8, 6, 8, 4, 9, 2, 0, 5, 3, 1, 3, 7, 1, 7, 6, 4, 8, 8, 9, 9, 8, 8, 9, 9, 8, 8, 9, 9, 2, 2, 3, 3, 0,
                     0,
                     1, 1, 4, 4, 5, 5, 2, 2, 3, 3],
                    [6, 8, 4, 8, 2, 9, 5, 0, 1, 3, 7, 3, 7, 1, 4, 6, 6, 7, 6, 7, 4, 5, 4, 5, 2, 3, 2, 3, 0, 1, 0, 1, 6,
                     7,
                     6, 7, 6, 7, 6, 7, 6, 7, 6, 7])  # 正向
            #                 topology = ([8, 6, 8, 4, 6, 4, 2, 9, 1, 3, 3, 7, 1, 7, 5, 0, 8, 8, 9, 9, 8, 8, 9, 9, 8, 8, 9, 9, 2, 2, 3, 3, 0, 0, 1, 1, 4, 4, 5, 5, 2, 2, 3, 3, 6, 7, 6, 7, 4, 5, 4, 5, 2, 3, 2, 3, 0, 1, 0, 1, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7],
            #                            [6, 8, 4, 8, 4, 6, 9, 2, 3, 1, 7, 3, 7, 1, 0, 5, 6, 7, 6, 7, 4, 5, 4, 5, 2, 3, 2, 3, 0, 1, 0, 1, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 6, 7, 8, 8, 9, 9, 8, 8, 9, 9, 8, 8, 9, 9, 2, 2, 3, 3, 0, 0, 1, 1, 4, 4, 5, 5, 2, 2, 3, 3])  # 使用异质图
            elif dataset == '20aiops':
                # topology = (
                #     [2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 13, 13, 13, 10, 10, 11, 11, 12, 12, 10, 11, 12],
                #     [1, 2, 6, 7, 8, 9, 1, 3, 6, 7, 8, 9, 1, 4, 6, 7, 8, 9, 1, 5, 6, 7, 8, 9, 0, 6, 0, 7, 0, 8, 0, 9, 4, 5, 13, 2, 6, 3, 7, 5, 9, 10, 11, 12])  # 正向
                topology = (
                    [1, 2, 6, 7, 8, 9, 1, 3, 6, 7, 8, 9, 1, 4, 6, 7, 8, 9, 1, 5, 6, 7, 8, 9, 0, 6, 0, 7, 0, 8, 0, 9, 4,
                     5, 13, 2, 6, 3, 7, 5, 9, 10, 11, 12],
                    [2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 13,
                     13, 13, 10, 10, 11, 11, 12, 12, 10, 11, 12])  # 反向
            elif dataset == 'aiops21':
                topology = ([12, 12, 13, 13, 0, 0, 0, 0, 1, 1, 1, 1, 8, 8, 9, 9, 10, 10, 11, 11, 8, 8, 9, 9, 10, 10, 11, 
                             11, 2, 2, 2, 2, 3, 3, 3, 3, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 14, 15, 16, 17, 
                             0, 1, 8, 9, 10, 11, 2, 3, 14, 15, 16, 17, 12, 13],
                            [0, 1, 0, 1, 8, 9, 10, 11, 8, 9, 10, 11, 6, 4, 6, 4, 6, 4, 6, 4, 2, 3, 2, 3, 2, 3, 2, 3, 14, 
                             15, 16, 17, 14, 15, 16, 17, 7, 7, 7, 7, 5, 5, 5, 5, 2, 2, 2, 2, 3, 3, 3, 3, 0, 1, 8, 9, 10, 
                             11, 2, 3, 14, 15, 16, 17, 12, 13])  # 正向
            elif dataset == 'aiops22':
                # topology = ([8, 6, 40, 22, 4, 20, 41, 8, 8, 21, 9, 22, 40, 10, 21, 21, 39, 23, 9, 20, 9, 10, 23, 9, 8, 10, 
                #              20, 9, 22, 20, 8, 21, 20, 10, 22, 21, 39, 10, 39, 20, 11, 22, 8, 22, 22, 8, 22, 10, 9, 9, 21, 
                #              9, 10, 8, 20, 38, 22, 21, 40, 22, 9, 9, 21, 8, 8, 23, 20, 41, 23, 8, 20, 20, 11, 8, 11, 22, 9, 
                #              20, 22, 9, 10, 10, 5, 20, 20, 21, 10, 20, 38, 22, 9, 8, 22, 41, 21, 10, 9, 21, 21, 21, 22, 23, 
                #              21, 10, 8, 41, 40, 10, 20, 20, 10, 20, 11, 20, 22, 8, 21, 22, 7, 10, 20, 11, 9, 20, 10, 21, 8, 
                #              39, 8, 22, 20, 21, 21, 22, 23, 9, 8, 10, 9, 38, 20, 23, 21, 21, 21, 21, 10, 9, 22, 23, 11, 38, 
                #              8, 11, 9, 22, 22, 10],
                #             [18, 6, 40, 14, 4, 8, 35, 35, 14, 8, 36, 12, 35, 36, 12, 40, 34, 45, 44, 4, 43, 31, 41, 32, 13, 
                #              42, 14, 34, 1, 39, 12, 43, 40, 5, 35, 5, 35, 43, 36, 5, 37, 6, 44, 10, 5, 16, 34, 35, 17, 6, 6, 
                #              30, 6, 42, 10, 35, 36, 36, 36, 43, 4, 31, 39, 6, 31, 15, 38, 41, 3, 4, 36, 12, 45, 8, 15, 0, 5, 
                #              20, 2, 13, 12, 18, 5, 43, 6, 38, 10, 9, 38, 22, 14, 36, 42, 34, 44, 14, 35, 2, 10, 4, 39, 23, 21, 
                #              16, 34, 36, 34, 34, 13, 42, 17, 44, 19, 0, 4, 43, 14, 8, 7, 30, 1, 33, 12, 2, 13, 0, 30, 39, 5, 
                #              40, 35, 35, 9, 44, 7, 9, 32, 4, 18, 34, 34, 37, 1, 13, 42, 34, 32, 42, 38, 11, 11, 36, 17, 7, 16, 
                #              9, 13, 44])
                topology = ([8, 6, 34, 22, 4, 20, 35, 8, 8, 21, 9, 22, 34, 10, 21, 21, 33, 23, 9, 20, 9, 10, 23, 9, 8, 10, 20, 
                             9, 22, 20, 8, 21, 20, 10, 22, 21, 33, 10, 33, 20, 11, 22, 8, 22, 22, 8, 22, 10, 9, 9, 21, 9, 10, 8, 
                             20, 32, 22, 21, 34, 22, 9, 9, 21, 8, 8, 23, 20, 35, 23, 8, 20, 20, 11, 8, 11, 22, 9, 20, 22, 9, 10, 
                             10, 5, 20, 20, 21, 10, 20, 32, 22, 9, 8, 22, 35, 21, 10, 9, 21, 21, 21, 22, 23, 21, 10, 8, 35, 34, 
                             10, 20, 20, 10, 20, 11, 20, 22, 8, 21, 22, 7, 10, 20, 11, 9, 20, 10, 21, 8, 33, 8, 22, 20, 21, 21, 
                             22, 23, 9, 8, 10, 9, 32, 20, 23, 21, 21, 21, 21, 10, 9, 22, 23, 11, 32, 8, 11, 9, 22, 22, 10],
                            [18, 6, 34, 14, 4, 8, 29, 29, 14, 8, 30, 12, 29, 30, 12, 34, 28, 39, 38, 4, 37, 25, 35, 26, 13, 36, 
                             14, 28, 1, 33, 12, 37, 34, 5, 29, 5, 29, 37, 30, 5, 31, 6, 38, 10, 5, 16, 28, 29, 17, 6, 6, 24, 6, 
                             36, 10, 29, 30, 30, 30, 37, 4, 25, 33, 6, 25, 15, 32, 35, 3, 4, 30, 12, 39, 8, 15, 0, 5, 20, 2, 13, 
                             12, 18, 5, 37, 6, 32, 10, 9, 32, 22, 14, 30, 36, 28, 38, 14, 29, 2, 10, 4, 33, 23, 21, 16, 28, 30, 
                             28, 28, 13, 36, 17, 38, 19, 0, 4, 37, 14, 8, 7, 24, 1, 27, 12, 2, 13, 0, 24, 33, 5, 34, 29, 29, 9, 
                             38, 7, 9, 26, 4, 18, 28, 28, 31, 1, 13, 36, 28, 26, 36, 32, 11, 11, 30, 17, 7, 16, 9, 13, 38])
            elif dataset == 'platform':
                topology = (
                    [1, 1, 1, 1, 7, 1, 1, 4, 4, 4, 4, 4, 1, 0, 2, 0, 3, 0, 4, 0,
                     5, 0, 6, 0, 7, 0, 8, 0, 9, 0, 1, 2, 1, 3, 1, 4, 1, 5, 1, 6, 1, 7, 1, 8, 1, 9, 2, 3, 2, 4, 2, 5, 2, 6,
                     2, 7, 2, 8, 2, 9, 3, 4, 3, 5, 3, 6, 3, 7, 3, 8, 3, 9, 4, 5, 4, 6, 4, 7, 4, 8, 4, 9, 5, 6, 5, 7, 5, 8, 
                     5, 9, 6, 7, 6, 8, 6, 9, 7, 8, 7, 9, 8, 9],
                    [0, 3, 6, 9, 6, 5, 2, 1, 2, 0, 6, 7, 0, 1, 0, 2, 0, 3, 0, 4,
                     0, 5, 0, 6, 0, 7, 0, 8, 0, 9, 2, 1, 3, 1, 4, 1, 5, 1, 6, 1, 7, 1, 8, 1, 9, 1, 3, 2, 4, 2, 5, 2, 6, 2,
                     7, 2, 8, 2, 9, 2, 4, 3, 5, 3, 6, 3, 7, 3, 8, 3, 9, 3, 5, 4, 6, 4, 7, 4, 8, 4, 9, 4, 6, 5, 7, 5, 8, 5,
                     9, 5, 7, 6, 8, 6, 9, 6, 8, 7, 9, 7, 9, 8])
            elif dataset == 'tt':
                topology = (
                    [2, 23, 13, 23, 23, 18, 2, 11, 2, 7, 1, 13, 23, 17, 2, 18, 20, 17],
                    [14, 11, 5, 20, 16, 10, 16, 19, 19, 6, 26, 18, 21, 11, 21, 11, 2, 4]
                    )
            else:
                raise Exception()
        return topology

    def get_edge_types(self):
        dataset = self.config['dataset']
        if not self.config['heterogeneous']:
            raise Exception()
        if dataset == 'gaia':
            etype = tensor(np.array(
                [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
                 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
                 2, 2, 2, 2]).astype(np.int64))
        elif dataset == '20aiops':
            etype = tensor(np.array(
                [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
                 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
                 2, 2, 2, 2, 2, 2, 2, 2]).astype(np.int64))
        elif dataset == '21aiops':
            etype = tensor(np.array(
                [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 
                 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 
                 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 
                 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]).astype(np.int64))
        else:
            raise Exception()
        return etype

class UnircaLab():
    def __init__(self, config):
        self.config = config
        instances = config['nodes'].split()
        self.ins_dict = dict(zip(instances, range(len(instances))))
        self.idx2instance = dict(zip(range(len(instances)), instances))
        self.demos = pd.read_csv(os.path.join(self.config['data_dir'], self.config['run_table']), index_col=0)
        # services = self.demos['service'].drop_duplicates().to_list()
        services = self.config["service"].split()
        services.sort()
        self.services = services
        # anomalys = self.demos['anomaly_type'].drop_duplicates().to_list()
        anomalys = [a.lstrip(' ') + ']' for a in self.config["anomaly"].split(']') if a != '' and a != ' ']
        anomalys.sort()
        self.anomalys = anomalys
        self.service_dict = dict(zip(services, range(len(services))))
        self.idx2service = dict(zip(range(len(services)), services))
        self.idx2anomaly = dict(zip(range(len(anomalys)), anomalys))
        if config['dataset'] == 'gaia':
            self.topoinfo = {0: [0, 1], 1: [2, 3], 2: [4, 5], 3: [6, 7], 4: [8, 9]}
        elif config['dataset'] == 'aiops21':
            self.topoinfo = {0: [0, 1], 1: [2, 3], 2: [4, 5], 3: [6, 7], 4: [8, 9, 10, 11], 5: [12, 13], 6: [14, 15], 7: [16, 17]}
        elif config['dataset'] == '20aiops':
            self.topoinfo = {0: [0, 1], 1: list(range(2, 10)), 2: list(range(10, 14))}
        elif config['dataset'] == 'aiops22':
            # self.topoinfo = {0: [0, 1, 2, 3], 1: [4,5,6,7], 2: [8,9,10,11], 3: [12,13,14,15], 4: [16,17,18,19], 
            #                  5: [20,21,22,23], 6: [24,25,26,27,28,29],7:[30,31,32,33],8:[34,35,36,37],
            #                  9:[38,39,40,41],10:[42,43,44,45]}
            self.topoinfo = {0: [0, 1, 2, 3], 1: [4,5,6,7], 2: [8,9,10,11], 3: [12,13,14,15], 4: [16,17,18,19], 
                        5: [20,21,22,23],6:[24,25,26,27],7:[28,29,30,31],
                        8:[32,33,34,35],9:[36,37,38,39]}
        elif config['dataset'] == 'platform':
            self.topoinfo = {0: [0], 1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6], 7: [7],
                             8: [8], 9: [9]}
        elif config['dataset'] == 'tt':
            self.topoinfo = {i: [i] for i in range(27)}
        else:
            raise Exception('Unknow dataset')

    def collate(self, samples):
        graphs, labels = map(list, zip(*samples))
        batched_graph = dgl.batch(graphs)
        batched_labels = torch.tensor(labels)
        return batched_graph, batched_labels

    def save_result(self, save_path, data):
        df = pd.DataFrame(data, columns=['top_k', 'accuracy'])
        df.to_csv(save_path, index=False)

    def multi_trainv0(self, model_ts_state_dict, model_ta_state_dict, dataset_ts, dataset_ta):
        if self.config['seed'] is not None:
            torch.manual_seed(self.config['seed'])
        weight = 0.5
        device = 'cpu'
        dataloader_ts = DataLoader(dataset_ts, batch_size=self.config['batch_size'], collate_fn=self.collate)
        dataloader_ta = DataLoader(dataset_ta, batch_size=self.config['batch_size'], collate_fn=self.collate)
        # in_dim_ts = dataset_ts.graphs[0].ndata['attr'].shape[1]
        in_dim_ts = dataset_ts[0][0].ndata['attr'].shape[1]
        # out_dim_ts = self.config['N_S']
        out_dim_ts = len(self.services)
        hid_dim_ts = (in_dim_ts + out_dim_ts) * 2 // 3
        # in_dim_ta = dataset_ta.graphs[0].ndata['attr'].shape[1]
        in_dim_ta = dataset_ta[0][0].ndata['attr'].shape[1]
        # out_dim_ta = self.config['N_A']
        out_dim_ta = len(self.anomalys)
        hid_dim_ta = (in_dim_ta + out_dim_ta) * 2 // 3
        if self.config['heterogeneous']:
            etype = U.load_info(os.path.join(self.config['save_dir'], 'edge_types.pkl'))
            model_ts = RGCNClassifier(in_dim_ts, hid_dim_ts, out_dim_ts, etype).to(device)
            model_ta = RGCNClassifier(in_dim_ta, hid_dim_ta, out_dim_ta, etype).to(device)
        else:
            model_ts = TAGClassifier(in_dim_ts, hid_dim_ts, out_dim_ts).to(device)
            model_ta = TAGClassifier(in_dim_ta, hid_dim_ta, out_dim_ta).to(device)
            # model = GCNClassifier(in_dim, hid_dim, out_dim).to(device)  # 同质图
#             model = GATClassifier(in_dim, hid_dim, out_dim, 3).to(device) # GAT
#             model = SAGEClassifier(in_dim, hid_dim, out_dim).to(device) # GraphSAGE
#             model = TAGClassifier(in_dim, hid_dim, out_dim) # TAGConv
#             model = GATv2Classifier(in_dim, hid_dim, out_dim, 3).to(device)
#             model = LinearClassifier(in_dim, hid_dim, out_dim).to(device)
#             model = ChebClassifier(in_dim, hid_dim, out_dim, 2, True).to(device) # ChebConv
            # model = SGCCClassifier(in_dim, hid_dim, out_dim).to(device)
        if model_ts_state_dict is not None or model_ta_state_dict is not None:
            print("use saved models")
            model_ts.load_state_dict(model_ts_state_dict)
            model_ta.load_state_dict(model_ta_state_dict)
        # print(model_ts)
        # print(model_ta)
        
        opt_ts = torch.optim.Adam(model_ts.parameters(), lr=self.config['lr'], weight_decay=self.config['weight_decay'])
        opt_ta = torch.optim.Adam(model_ta.parameters(), lr=self.config['lr'], weight_decay=self.config['weight_decay'])
        losses = []
        model_ts.train()
        model_ta.train()
        
        ts_samples = [(batched_graphs, labels) for batched_graphs, labels in dataloader_ts]
        ta_samples = [(batched_graphs, labels) for batched_graphs, labels in dataloader_ta]
        for epoch in tqdm(range(self.config['epoch'])):
            epoch_loss = 0
            epoch_cnt = 0
            features = []
            for i in range(len(ts_samples)):
                # service
                ts_bg = ts_samples[i][0].to(device)
                ts_labels = ts_samples[i][1].to(device)
                ts_feats = ts_bg.ndata['attr'].float()
                ts_logits = model_ts(ts_bg, ts_feats)
                ts_loss = F.cross_entropy(ts_logits, ts_labels)
                # anomaly_type
                ta_bg = ta_samples[i][0].to(device)
                ta_labels = ta_samples[i][1].to(device)
                ta_feats = ta_bg.ndata['attr'].float()
                ta_logits = model_ta(ta_bg, ta_feats)
                ta_loss = F.cross_entropy(ta_logits, ta_labels)
                
                opt_ts.zero_grad()
                opt_ta.zero_grad()
                
                total_loss = weight*ts_loss+(1-weight)*ta_loss
                total_loss.backward()
                opt_ts.step()
                opt_ta.step()
                epoch_loss += total_loss.detach().item()
                epoch_cnt += 1
            losses.append(epoch_loss / epoch_cnt)
            if len(losses) > self.config['win_size'] and \
                    abs(losses[-self.config['win_size']] - losses[-1]) < self.config['win_threshold']:
                break
        return model_ts, model_ta  

    # 获取训练集和测试集的编码
    def get_embedings(self, model, train_dataset, test_dataset):
        model.eval()
        trainloader = DataLoader(train_dataset, batch_size=len(train_dataset) + 10, collate_fn=self.collate)
        testloader = DataLoader(test_dataset, batch_size=len(test_dataset) + 10, collate_fn=self.collate)
        for batched_graph, labels in trainloader:
            train_embeds = model.get_embeds(batched_graph, batched_graph.ndata['attr'].float())
        
        for batched_graph, labels in testloader:
            test_embeds = model.get_embeds(batched_graph, batched_graph.ndata['attr'].float())
        dataset = self.config['dataset']
        with open(f'results/{dataset}_train_embeds.pkl', 'wb') as f:
            pickle.dump(train_embeds, f)
        with open(f'results/{dataset}_test_embeds.pkl', 'wb') as f:
            pickle.dump(test_embeds, f)
        return
    
    def testv2(self, model, dataset, task, out_file, save_file=None):
        model.eval()
        dataloader = DataLoader(dataset, batch_size=len(dataset) + 10, collate_fn=self.collate)
        device = 'cpu'
        seed = self.config['seed']
        accuracy = []
        for batched_graph, labels in dataloader:
            batched_graph = batched_graph.to(device)
            labels = labels.to(device)
            output = model(batched_graph, batched_graph.ndata['attr'].float())
            k = 5 if output.shape[-1] >= 5 else output.shape[-1]
            if task == 'instance':
                _, indices = torch.topk(output, k=k, dim=1, largest=True, sorted=True)  
                out_dir = os.path.join(self.config['save_dir'], 'preds')
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                y_pred = indices.detach().numpy()
                y_true = labels.detach().numpy().reshape(-1, 1)
                ser_res = pd.DataFrame(np.append(y_pred, y_true, axis=1), 
                                       columns=np.append([f'Top{i}' for i in range(1, len(y_pred[0])+1)], 'GroundTruth'))
                
                # 定位到实例级别
                accs, ins_res = self.test_instance_local(ser_res, max_num=2)
                ins_res.to_csv(f'{out_dir}/multitask_seed{seed}_{out_file}')
                columns = ['A@1', 'A@2', 'A@3', 'A@4', 'A@5']
            elif task == 'anomaly_type':
                _, indices = torch.topk(output, k=k, dim=1, largest=True, sorted=True)  
                out_dir = os.path.join(self.config['save_dir'], 'preds')
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                y_pred = indices.detach().numpy()
                y_true = labels.detach().numpy().reshape(-1, 1)
                pre = precision_score(y_pred[:, 0], y_true, average='weighted')
                rec = recall_score(y_pred[:, 0], y_true, average='weighted')
                f1 = f1_score(y_pred[:, 0], y_true, average='weighted')
                print('Weighted precision', pre)
                print('Weighted recall', rec)
                print('Weighted f1-score', f1)
                with open(self.config['anomaly_result_path'], 'a+', encoding='utf8') as w:
                    w.write(
                        "weight,%s,%s,%s\n" % (str(pre), str(rec), str(f1))
                    )
                pre = precision_score(y_pred[:, 0], y_true, average='micro')
                rec = recall_score(y_pred[:, 0], y_true, average='micro')
                f1 = f1_score(y_pred[:, 0], y_true, average='micro')
                print('Micro precision', pre)
                print('Micro recall', rec)
                print('Micro f1-score', f1)
                with open(self.config['anomaly_result_path'], 'a+', encoding='utf8') as w:
                    w.write(
                        "micro,%s,%s,%s\n" % (str(pre), str(rec), str(f1))
                    )
                pre = precision_score(y_pred[:, 0], y_true, average='macro')
                rec = recall_score(y_pred[:, 0], y_true, average='macro')
                f1 = f1_score(y_pred[:, 0], y_true, average='macro')
                print('Macro precision', pre)
                print('Macro recall', rec)
                print('Macro f1-score', f1)
                # 记录实验结果
                # ---------
                with open(self.config['anomaly_result_path'], 'a+', encoding='utf8') as w:
                    w.write(
                        "macro,%s,%s,%s\n" % (str(pre), str(rec), str(f1))
                    )
                # ---------
                test_cases = self.demos[self.demos['data_type']=='test']
                pd.DataFrame(np.append(
                    y_pred[:, 0].reshape(-1, 1), y_true, axis=1), columns=[
                                           'Pred', 'GroundTruth'], index=test_cases.index).to_csv(
                                               f'{out_dir}/multitask_seed{seed}_{out_file}')
                columns = ['Precision', 'Recall', 'F1-Score']
                accs = np.array([pre, rec, f1])
            else:
                raise Exception('Unknow task')

        if save_file:
            accuracy = pd.DataFrame(accs.reshape(-1, len(columns)), columns=columns)
            save_dir = os.path.join(self.config['save_dir'], 'evaluations', save_file.split('_')[0])
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            self.save_result(f'{save_dir}/seed{seed}_{save_file}', accuracy)

        return output, labels

    def test_instance_local_rt_topk(self, s_preds, max_num=2):
        """
        根据微服务的预测结果预测微服务的根因实例
        """
        with open(self.config['text_path'], 'rb') as f:
            info = pickle.load(f)
        ktype = type(list(info.keys())[0])
        test_cases = self.demos[self.demos['data_type']=='test']
        ins_topks = np.zeros(5)
        s_topks = np.zeros(5)
        ins_preds = []
        i = 0
        for index, row in test_cases.iterrows():
            index = ktype(index)
            num_dict = {}
            for pair in info[index]:
                num_dict[self.ins_dict[pair[0]]] = len(info[index][pair].split())
            s_pred = s_preds.loc[i]
            # s_pred 是微服务label预测结果
            ins_pred = []
            for col in list(s_preds.columns)[: -1]:
                temp = sorted([(ins_id, num_dict[ins_id]) for ins_id in self.topoinfo[s_pred[col]]],
                              key=lambda x: x[-1], reverse=True)
                # temp = [(实例id，该实例在当前case下的事件数量),...]
                # 按事件数量排序
                # print(self.topoinfo[s_pred[col]], temp)
                ins_pred.extend([item[0] for item in temp[: max_num]])
            ins_preds.append(ins_pred[: 5])
            for k in range(5):
                if self.config["dataset"] == "aiops22":
                    if row["level"] == "service":
                        if self.idx2instance[ins_pred[k]].startswith(row["service"]):
                            ins_topks[k: ] += 1
                            break
                    else:
                        if ins_pred[k] == self.ins_dict[row['instance']]:
                            ins_topks[k: ] += 1
                            break
                else:
                    if ins_pred[k] == self.ins_dict[row['instance']]:
                        ins_topks[k: ] += 1
                        break
            for j in range(5):
                if s_pred[j] == self.service_dict[row['service']]:
                    s_topks[j:] += 1
                    break
            i += 1
        print('(service )Top1-5: ', s_topks / len(test_cases))
        print('(instance)Top1-5: ', ins_topks/len(test_cases))
        return s_topks / len(test_cases), ins_topks/len(test_cases)
    
    def test_rt_metric(self, model, dataset, task, out_file, save_file=None):
        model.eval()
        dataloader = DataLoader(dataset, batch_size=len(dataset) + 10, collate_fn=self.collate)
        device = 'cpu'
        seed = self.config['seed']
        accuracy = []
        for batched_graph, labels in dataloader:
            batched_graph = batched_graph.to(device)
            labels = labels.to(device)
            output = model(batched_graph, batched_graph.ndata['attr'].float())
            test_loss = torch.nn.functional.cross_entropy(output, labels)
            print("\033[35mTEST LOSS:%.5f\033[0m" % test_loss.item())
            k = 5 if output.shape[-1] >= 5 else output.shape[-1]
            if task == 'instance':
                _, indices = torch.topk(output, k=k, dim=1, largest=True, sorted=True)  
                out_dir = os.path.join(self.config['save_dir'], 'preds')
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                y_pred = indices.detach().numpy()
                y_true = labels.detach().numpy().reshape(-1, 1)
                ser_res = pd.DataFrame(np.append(y_pred, y_true, axis=1), 
                                       columns=np.append([f'Top{i}' for i in range(1, len(y_pred[0])+1)], 'GroundTruth'))
                
                # 定位到实例级别
                s_topk, i_topk = self.test_instance_local_rt_topk(ser_res, max_num=2)
                metrics = s_topk.tolist()
                metrics.extend(i_topk.tolist())
                return metrics
            elif task == 'anomaly_type':
                _, indices = torch.topk(output, k=k, dim=1, largest=True, sorted=True)  
                out_dir = os.path.join(self.config['save_dir'], 'preds')
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                y_pred = indices.detach().numpy()
                y_true = labels.detach().numpy().reshape(-1, 1)
                metrics = []
                pre = precision_score(y_pred[:, 0], y_true, average='weighted')
                rec = recall_score(y_pred[:, 0], y_true, average='weighted')
                f1 = f1_score(y_pred[:, 0], y_true, average='weighted')
                metrics.append(pre)
                metrics.append(rec)
                metrics.append(f1)
                print('Weighted precision', pre)
                print('Weighted recall', rec)
                print('Weighted f1-score', f1)
                with open(self.config['anomaly_result_path'], 'a+', encoding='utf8') as w:
                    w.write(
                        "weight,%s,%s,%s\n" % (str(pre), str(rec), str(f1))
                    )
                pre = precision_score(y_pred[:, 0], y_true, average='micro')
                rec = recall_score(y_pred[:, 0], y_true, average='micro')
                f1 = f1_score(y_pred[:, 0], y_true, average='micro')
                metrics.append(pre)
                metrics.append(rec)
                metrics.append(f1)
                print('Micro precision', pre)
                print('Micro recall', rec)
                print('Micro f1-score', f1)
                with open(self.config['anomaly_result_path'], 'a+', encoding='utf8') as w:
                    w.write(
                        "micro,%s,%s,%s\n" % (str(pre), str(rec), str(f1))
                    )
                pre = precision_score(y_pred[:, 0], y_true, average='macro')
                rec = recall_score(y_pred[:, 0], y_true, average='macro')
                f1 = f1_score(y_pred[:, 0], y_true, average='macro')
                metrics.append(pre)
                metrics.append(rec)
                metrics.append(f1)
                print('Macro precision', pre)
                print('Macro recall', rec)
                print('Macro f1-score', f1)
                return metrics, y_pred[:, 0], y_true
            else:
                raise Exception('Unknow task')

    def test_instance_local(self, s_preds, max_num=2):
        """
        根据微服务的预测结果预测微服务的根因实例
        """
        with open(self.config['text_path'], 'rb') as f:
            info = pickle.load(f)
        ktype = type(list(info.keys())[0])
        test_cases = self.demos[self.demos['data_type']=='test']
        ins_topks = np.zeros(5)
        s_topks = np.zeros(5)
        ins_preds = []
        i = 0
        for index, row in test_cases.iterrows():
            index = ktype(index)
            num_dict = {}
            for pair in info[index]:
                num_dict[self.ins_dict[pair[0]]] = len(info[index][pair].split())
            s_pred = s_preds.loc[i]
            # s_pred 是微服务label预测结果
            ins_pred = []
            for col in list(s_preds.columns)[: -1]:
                temp = sorted([(ins_id, num_dict[ins_id]) for ins_id in self.topoinfo[s_pred[col]]],
                              key=lambda x: x[-1], reverse=True)
                # temp = [(实例id，该实例在当前case下的事件数量),...]
                # 按事件数量排序
                # print(self.topoinfo[s_pred[col]], temp)
                ins_pred.extend([item[0] for item in temp[: max_num]])
            ins_preds.append(ins_pred[: 5])
            for k in range(5):
                if self.config["dataset"] == "aiops22":
                    if row["level"] == "service":
                        if self.idx2instance[ins_pred[k]].startswith(row["service"]):
                            ins_topks[k: ] += 1
                            break
                    else:
                        if ins_pred[k] == self.ins_dict[row['instance']]:
                            ins_topks[k: ] += 1
                            break
                else:
                    if ins_pred[k] == self.ins_dict[row['instance']]:
                        ins_topks[k: ] += 1
                        break
            for j in range(5):
                if s_pred[j] == self.service_dict[row['service']]:
                    s_topks[j:] += 1
                    break
            i += 1
        print('(service )Top1-5: ', s_topks / len(test_cases))
        print('(instance)Top1-5: ', ins_topks/len(test_cases))
        # 记录实验结果
        # ----------
        with open(self.config['topk_path'], 'a+', encoding='utf8') as w:
            w.write(
                "service," + ",".join([str(rt) for rt in s_topks / len(test_cases)]) + "\n" +
                "instance," + ",".join([str(rt) for rt in ins_topks/len(test_cases)]) + "\n"
            )
        # ----------
        y_true = np.array([self.ins_dict[ins] for ins in test_cases['instance'].values]).reshape(-1, 1)
        return ins_topks/len(test_cases), pd.DataFrame(np.append(
            ins_preds, y_true, axis=1), columns=[
                'Top1', 'Top2', 'Top3', 'Top4', 'Top5', 'GroundTruth'], index=test_cases.index)

    def calc_instance_anomaly_tuple(self, s_output, s_labels, a_output, a_labels):
        # N_S = self.config['N_S']
        N_S = len(self.services)
        # N_A = self.config['N_A']
        N_A = len(self.anomalys)
        TOPK_SA = self.config['TOPK_SA']
        with open(self.config['text_path'], 'rb') as f:
            info = pickle.load(f)
        test_cases = self.demos[self.demos['data_type']=='test']
        ins_event_num_list = []
        i = 0
        for index, row in test_cases.iterrows():
            num_dict = {}
            for pair in info[index]:
                num_dict[self.ins_dict[pair[0]]] = len(info[index][pair].split())
            ins_event_num_list.append((index, num_dict))
        # softmax取正（使用笛卡尔积比大小）
        s_values = nn.Softmax(dim=1)(s_output)
        a_values = nn.Softmax(dim=1)(a_output)
        # 获得 K_ * K_的笛卡尔积
        product = []
        for k in range(len(s_values)):
            service_val = s_values[k]
            anomaly_val = a_values[k]
            m = torch.zeros(N_S * N_A).reshape(N_S, N_A)
            for i in range(N_S):
                for j in range(N_A):
                    m[i][j] = service_val[i] * anomaly_val[j]
            product.append(m)
        # 获得每个笛卡尔积矩阵的topk及坐标
        writer = open(self.config['tuple_path'], 'w', encoding='utf8')
        writer.write(
            "case,topk,service,instance,anomaly\n"
        )
        sa_topks = []
        for idx in range(len(product)):
            m = product[idx]
            topk = []
            last_max_val = 1
            for k in range(TOPK_SA):
                cur_max_val = tensor(0)
                x = 0
                y = 0
                for i in range(N_S):
                    for j in range(N_A):
                        if m[i][j] > cur_max_val and m[i][j] < last_max_val:
                            cur_max_val = m[i][j]
                            x = i
                            y = j
                topk.append(((x, y), cur_max_val.item()))
                last_max_val = cur_max_val
            case, num_dict = ins_event_num_list[idx]
            
            for k, _topk in enumerate(topk):
                s_idx, a_idx = _topk[0]

                temp = sorted([(ins_id, num_dict[ins_id]) for ins_id in self.topoinfo[s_idx]],
                              key=lambda x: x[-1], reverse=True)

                row = "%s,%s,%s,%s,%s\n" %(str(case),str(k),self.idx2service[s_idx],self.idx2instance[temp[0][0]],self.idx2anomaly[a_idx])
                writer.write(row)
            sa_topks.append(topk)
        writer.close()
        return sa_topks

    def do_lab(self, lab_id):
        save_dir = os.path.join(self.config['save_dir'], str(lab_id))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        self.config['save_dir'] = save_dir
        
        RawDataProcess(self.config, self.services, self.anomalys).process()
        # 训练
        t1 = time.time()
        print('train starts at', t1)
        model_ts, model_ta = self.multi_trainv0(
            None,
            None,
            UnircaDataset(
                os.path.join(save_dir, 'train_Xs.pkl'),
                os.path.join(save_dir, 'train_ys_service.pkl'),
                os.path.join(save_dir, 'topology.pkl'),
                aug=self.config['aug'],
                aug_size=self.config['aug_size'],
                shuffle=True), 
            UnircaDataset(os.path.join(save_dir, 'train_Xs.pkl'),
                os.path.join(save_dir, 'train_ys_anomaly_type.pkl'),
                os.path.join(save_dir, 'topology.pkl'),
                aug=self.config['aug'],
                aug_size=self.config['aug_size'],
                shuffle=True)
            )
        t2 = time.time()
        print('train ends at', t2)
        print('train use time',t2 - t1, 's')
        # 测试并分析准确率
        s = time.time()
        print('test starts at', s)
        print('[Multi_task learning v0]')
#         t_output, t_labels = self.test(trans_model,
#                                        UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
#                                                      os.path.join(save_dir, 'test_ys_service.pkl'),
#                                                      os.path.join(save_dir, 'topology.pkl')),
#                                        'service_pred_trans.csv',
#                                        'service_acc_trans.csv')
        print('instance')
        s_outputs, s_labels = self.testv2(model_ts,
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_service.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'instance',
                                       'instance_pred_multi_v0.csv',
                                       'instance_acc_multi_v0.csv')
        print('anomaly type')
        a_outputs, a_labels = self.testv2(model_ta,
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_anomaly_type.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'anomaly_type',
                                       'anomaly_pred_multi_v0.csv',
                                       'anomaly_acc_multi_v0.csv')
        # 二元组
        print('calculate tuple')
        self.calc_instance_anomaly_tuple(
            s_outputs,
            s_labels,
            a_outputs,
            a_labels
        )
        print('caculate tuple finished')
        t = time.time()
        print('test ends at', t)
        print('test use time', t - s, 's')
        # 保存模型
        if self.config['save_model']:
            torch.save(model_ts, os.path.join(save_dir, 'service_model.pt'))
            torch.save(model_ta, os.path.join(save_dir, 'anomaly_type_model.pt'))


    def do_test(self, lab_id):
        save_dir = os.path.join(self.config['save_dir'], str(lab_id))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        self.config['save_dir'] = save_dir
        
        RawDataProcess(self.config, self.services, self.anomalys).process()
        # 测试并分析准确率
        s = time.time()
        print('test starts at', s)
        print('[Multi_task learning v0]')
        print('instance')
        s_outputs, s_labels = self.testv2(torch.load(os.path.join(save_dir, 'service_model.pt')),
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_service.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'instance',
                                       'instance_pred_multi_v0.csv',
                                       'instance_acc_multi_v0.csv')
        print('anomaly type')
        a_outputs, a_labels = self.testv2(torch.load(os.path.join(save_dir, 'anomaly_type_model.pt')),
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_anomaly_type.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'anomaly_type',
                                       'anomaly_pred_multi_v0.csv',
                                       'anomaly_acc_multi_v0.csv')
        # 二元组
        print('calculate tuple')
        self.calc_instance_anomaly_tuple(
            s_outputs,
            s_labels,
            a_outputs,
            a_labels
        )
        print('caculate tuple finished')
        t = time.time()
        print('test ends at', t)
        print('test use time', t - s, 's')
        # 保存模型


    def train_test(self, lab_id):
        save_dir = os.path.join(self.config['save_dir'], str(lab_id))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        self.config['save_dir'] = save_dir
        RawDataProcess(self.config, self.services, self.anomalys).process()
        ts_dataset = UnircaDataset(os.path.join(save_dir, 'train_Xs.pkl'),
                                    os.path.join(save_dir, 'train_ys_service.pkl'),
                                    os.path.join(save_dir, 'topology.pkl'),
                                    aug=self.config['aug'],
                                    aug_size=self.config['aug_size'],
                                    shuffle=True)
        ta_dataset = UnircaDataset(os.path.join(save_dir, 'train_Xs.pkl'),
                                    os.path.join(save_dir, 'train_ys_anomaly_type.pkl'),
                                    os.path.join(save_dir, 'topology.pkl'),
                                    aug=self.config['aug'],
                                    aug_size=self.config['aug_size'],
                                    shuffle=True)
        model_ts, model_ta = self.multi_trainv0(
            None,
            None,
            ts_dataset,
            ta_dataset
        )
        metrics = []
        print('instance')
        _metrics = self.test_rt_metric(model_ts,
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_service.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'instance',
                                       'instance_pred_multi_v0.csv',
                                       'instance_acc_multi_v0.csv')
        metrics.extend(_metrics)
        print('anomaly type')
        _metrics, y_pred, y_true = self.test_rt_metric(model_ta,
                                       UnircaDataset(os.path.join(save_dir, 'test_Xs.pkl'),
                                                     os.path.join(save_dir, 'test_ys_anomaly_type.pkl'),
                                                     os.path.join(save_dir, 'topology.pkl')),
                                       'anomaly_type',
                                       'anomaly_pred_multi_v0.csv',
                                       'anomaly_acc_multi_v0.csv')
        
        metrics.extend(_metrics)
        return metrics, y_pred.flatten(), y_true.flatten()