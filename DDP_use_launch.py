import argparse
import random
from dataclasses import dataclass
import numpy as np
import torch
import torch.optim as optim
import torch.nn as nn
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from datetime import datetime
import platform

from models import DevModel
from data import TrainDataSet
from utils import *


@dataclass
# 训练、评估、预测设置
class TrainConfig(nn.Module):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    vocab_size = 65
    # 训练设置
    train_data_proportion = 0.9
    batch_size = 64
    iterations = 5000
    learning_rate = 3e-4
    eval_interval = 500
    eval_iters = 200
    max_tokens = 500


parser = argparse.ArgumentParser()
parser.add_argument("--local-rank", type=int, default=-1)
args = parser.parse_args()

torch.cuda.set_device(args.local_rank)
device = torch.device('cuda', args.local_rank)

if platform.system() == 'Linux':
    backend='nccl'  # Linux系统使用NCCL
elif platform.system() == 'Windows':
    backend='gloo'  # Windows系统使用Gloo
else:
    raise ValueError("Unsupported OS")
torch.distributed.init_process_group(backend=backend)

# 固定随机种子
seed = 42
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)

model = DevModel(TrainConfig)
model.to(device)
optimizer = optim.AdamW(model.parameters(), lr=3e-4)

# 加载数据集
input_file_path = "data/input.txt"
with open(input_file_path, 'r', encoding='utf-8') as f:
    data = f.read()

# 序列化并划分数据集
chars = sorted(list(set(data)))
s2i = {ch: i for i, ch in enumerate(chars)}
i2s = {i: ch for i, ch in enumerate(chars)}
encoder = lambda s: [s2i[c] for c in s]
decoder = lambda nums: "".join([i2s[num] for num in nums])
input_sequence = torch.tensor(encoder(data), dtype=torch.long)
n = int(0.9 * len(input_sequence))

train_data = TrainDataSet(input_sequence[:n], model.ModelStruct.block_size)
train_sampler = DistributedSampler(train_data)
train_loader = torch.utils.data.DataLoader(train_data, sampler=train_sampler, batch_size=TrainConfig.batch_size)
# 计算参数数量
total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"模型参数量：{total_params}.")

model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank], output_device=args.local_rank,
                                                  find_unused_parameters=True)

total_batches = len(train_loader)
# training!
for i, data in tqdm(enumerate(train_loader), total=total_batches, desc="Training"):
    inputs, labels = data
    # forward
    inputs = inputs.to(device)
    labels = labels.to(device)
    _, loss = model(inputs, labels)
    # backward
    loss.backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    # log
    if args.local_rank == 0 and i % 100 == 0:
        print(f"loss = {loss}")

if args.local_rank == 0:
    # 仅保存模型参数
    torch.save(model.module.state_dict(),"checkpoint/" + datetime.now().strftime('%Y-%m-%d-%H-%M-%S') + f"-params-{total_params}" + ".pth")
    print(f"模型保存成功.")