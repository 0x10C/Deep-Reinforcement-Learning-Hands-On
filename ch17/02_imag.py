#!/usr/bin/env python3
import os
import gym
import ptan
import argparse
import numpy as np
from tensorboardX import SummaryWriter

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

from lib import common, i2a


LEARNING_RATE = 1e-4
NUM_ENVS = 16
BATCH_SIZE = 64


def iterate_batches(envs, net, cuda=False):
    act_selector = ptan.actions.ProbabilityActionSelector()
    mb_obs = np.zeros((BATCH_SIZE, ) + common.IMG_SHAPE, dtype=np.uint8)
    mb_probs = np.zeros((BATCH_SIZE, envs[0].action_space.n), dtype=np.float32)
    mb_obs_next = np.zeros((BATCH_SIZE, ) + common.IMG_SHAPE, dtype=np.uint8)
    mb_actions = np.zeros((BATCH_SIZE, ), dtype=np.int32)
    obs = [e.reset() for e in envs]
    batch_idx = 0

    while True:
        obs_v = ptan.agent.default_states_preprocessor(obs, cuda=cuda)
        logits_v, values_v = net(obs_v)
        probs_v = F.softmax(logits_v)
        probs = probs_v.data.cpu().numpy()
        actions = act_selector(probs)

        for e_idx, e in enumerate(envs):
            o, r, done, _ = e.step(actions[e_idx])
            mb_obs[batch_idx] = obs[e_idx]
            mb_probs[batch_idx] = probs[e_idx]
            mb_obs_next[batch_idx] = o
            mb_actions[batch_idx] = actions[e_idx]

            batch_idx = (batch_idx + 1) % BATCH_SIZE
            if batch_idx == 0:
                yield mb_obs, mb_probs
            if done:
                o = e.reset()
            obs[e_idx] = o

    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    parser.add_argument("-m", "--model", required=True, help="File with model to load")
    args = parser.parse_args()

    saves_path = os.path.join("saves", "02_env_" + args.name)
    os.makedirs(saves_path, exist_ok=True)

    make_env = lambda: ptan.common.wrappers.wrap_dqn(gym.make("BreakoutNoFrameskip-v4"))
    envs = [make_env() for _ in range(NUM_ENVS)]
    writer = SummaryWriter(comment="-02_env_" + args.name)

    net = common.AtariA2C(envs[0].observation_space.shape, envs[0].action_space.n)
    net_imag_policy = i2a.ImagPolicy(envs[0].observation_space.shape, envs[0].action_space.n)
    net.load_state_dict(torch.load(args.model))
    if args.cuda:
        net.cuda()
        net_imag_policy.cuda()
    print(net_imag_policy)
    optimizer = optim.Adam(net_imag_policy.parameters(), lr=LEARNING_RATE)

    step_idx = 0
    best_reward = None
    with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
        for mb_obs, mb_probs in iterate_batches(envs, net, cuda=args.cuda):
            obs_v = Variable(torch.from_numpy(mb_obs))
            probs_v = Variable(torch.from_numpy(mb_probs))
            if args.cuda:
                obs_v = obs_v.cuda()
                probs_v = probs_v.cuda()

            optimizer.zero_grad()
            imag_policy_logits_v = net_imag_policy(obs_v)
            imag_policy_loss_v = -F.log_softmax(imag_policy_logits_v) * probs_v
            imag_policy_loss_v = imag_policy_loss_v.sum(dim=1).mean()
            imag_policy_loss_v.backward()
            optimizer.step()
            tb_tracker.track("imag_policy_loss", imag_policy_loss_v, step_idx)

            step_idx += 1
