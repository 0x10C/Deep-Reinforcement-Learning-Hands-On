#!/usr/bin/env python3
import gym
import collections
import numpy as np
from tensorboardX import SummaryWriter

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable


HIDDEN_SIZE = 128
BATCH_SIZE = 16
PERCENTILLE = 50


class Net(nn.Module):
    def __init__(self, obs_size, hidden_size, n_actions):
        super(Net, self).__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, n_actions)
        )

    def forward(self, x):
        return self.net(x)


BatchExample = collections.namedtuple('BatchExample', field_names=['reward', 'episode'])
EpisodeStep  = collections.namedtuple('EpisodeStep', field_names=['observation', 'action'])


def iterate_batches(env, net, batch_size):
    batch = []
    episode_reward = 0.0
    episode_samples = []
    obs = env.reset()
    sm = nn.Softmax()
    while True:
        obs_v = Variable(torch.FloatTensor([obs]))
        act_probs = sm(net(obs_v)).data.cpu().numpy()[0]
        action = np.random.choice(len(act_probs), p=act_probs)
        next_obs, reward, is_done, _ = env.step(action)
        episode_reward += reward
        episode_samples.append(EpisodeStep(observation=obs, action=action))
        if is_done:
            batch.append(BatchExample(reward=episode_reward, episode=episode_samples))
            if len(batch) == batch_size:
                yield batch
                batch = []
            episode_reward = 0.0
            episode_samples = []
            next_obs = env.reset()
        obs = next_obs


def filter_batch(batch, percentille):
    rewards = list(map(lambda s: s.reward, batch))
    reward_bound = np.percentile(rewards, percentille)
    reward_mean = float(np.mean(rewards))

    train_obs = []
    train_act = []
    for example in batch:
        if example.reward < reward_bound:
            continue
        train_obs.extend(map(lambda step: step.observation, example.episode))
        train_act.extend(map(lambda step: step.action, example.episode))

    return Variable(torch.FloatTensor(train_obs)), Variable(torch.LongTensor(train_act)), reward_bound, reward_mean


if __name__ == "__main__":
    env = gym.make("CartPole-v0")
    obs_size = env.observation_space.shape[0]
    n_actions = env.action_space.n

    net = Net(obs_size, HIDDEN_SIZE, n_actions)
    objective = nn.CrossEntropyLoss()
    optimizer = optim.Adam(params=net.parameters(), lr=0.01)
    writer = SummaryWriter()

    for iter_no, batch in enumerate(iterate_batches(env, net, BATCH_SIZE)):
        train_obs, train_acts, reward_bound, reward_mean = filter_batch(batch, PERCENTILLE)
        optimizer.zero_grad()
        action_scores = net(train_obs)
        loss_v = objective(action_scores, train_acts)
        loss_v.backward()
        optimizer.step()
        print("%d: loss=%.3f, reward_mean=%.1f, reward_bound=%.1f" % (
            iter_no, loss_v.data[0], reward_mean, reward_bound))
        writer.add_scalar("loss", loss_v.data[0], iter_no)
        writer.add_scalar("reward_bound", reward_bound, iter_no)
        writer.add_scalar("reward_mean", reward_mean, iter_no)
        if reward_bound > 199:
            print("Solved!")
            break
    writer.close()
