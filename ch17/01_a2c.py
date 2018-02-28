#!/usr/bin/env python3
import gym
import ptan
import argparse
import numpy as np
from tensorboardX import SummaryWriter

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.nn.utils as nn_utils
from torch.autograd import Variable

from lib import common


LEARNING_RATE = 7e-4
NUM_ENVS = 16

REWARD_BOUND = 400


def discount_with_dones(rewards, dones, gamma):
    discounted = []
    r = 0
    for reward, done in zip(rewards[::-1], dones[::-1]):
        r = reward + gamma*r*(1.-done)
        discounted.append(r)
    return discounted[::-1]


def iterate_train_batches(envs, net, cuda=False):
    act_selector = ptan.actions.ProbabilityActionSelector()
    obs = [e.reset() for e in envs]
    cur_dones = [False] * NUM_ENVS
    mb_obs = np.zeros((NUM_ENVS, common.REWARD_STEPS) + common.IMG_SHAPE, dtype=np.uint8)
    mb_rewards = np.zeros((NUM_ENVS, common.REWARD_STEPS), dtype=np.float32)
    mb_values = np.zeros((NUM_ENVS, common.REWARD_STEPS), dtype=np.float32)
    mb_dones = np.zeros((NUM_ENVS, common.REWARD_STEPS), dtype=np.bool)
    mb_actions = np.zeros((NUM_ENVS, common.REWARD_STEPS), dtype=np.int32)

    while True:
        for n in range(common.REWARD_STEPS):
            obs_v = ptan.agent.default_states_preprocessor(obs)
            mb_obs[:, n] = obs_v.data.numpy()
            mb_dones[:,  n] = cur_dones
            if cuda:
                obs_v = obs_v.cuda()
            logits_v, values_v = net(obs_v)
            probs_v = F.softmax(logits_v)
            actions = act_selector(probs_v.data.cpu().numpy())
            mb_actions[:, n] = actions
            mb_values[:, n] = values_v.squeeze().data.cpu().numpy()
            for e_idx, e in enumerate(envs):
                o, r, done, _ = e.step(actions[e_idx])
                if done:
                    o = e.reset()
                obs[e_idx] = o
                mb_rewards[e_idx, n] = r
                cur_dones[e_idx] = done
        # obtain values for the last observation
        obs_v = ptan.agent.default_states_preprocessor(obs, cuda)
        _, values_v = net(obs_v)
        values_last = values_v.squeeze().data.cpu().numpy()
        # prepare before rollouts calculation
        mb_dones = np.roll(mb_dones, -1, axis=1)
        mb_dones[:, -1] = cur_dones

        for e_idx, (rewards, dones, value) in enumerate(zip(mb_rewards, mb_dones, values_last)):
            rewards = rewards.tolist()
            dones = dones.tolist()
            if not dones[-1]:
                rewards = discount_with_dones(rewards + [value], dones + [False], common.GAMMA)[:-1]
            else:
                rewards = discount_with_dones(rewards, dones, common.GAMMA)
            mb_rewards[e_idx] = rewards

        out_mb_obs = mb_obs.reshape((-1,) + common.IMG_SHAPE)
        out_mb_rewards = mb_rewards.flatten()
        out_mb_actions = mb_actions.flatten()
        out_mb_values = mb_values.flatten()
        yield out_mb_obs, out_mb_rewards, out_mb_actions, out_mb_values



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    make_env = lambda: ptan.common.wrappers.wrap_dqn(gym.make("BreakoutNoFrameskip-v4"))
    envs = [make_env() for _ in range(NUM_ENVS)]
    writer = SummaryWriter(comment="-breakout-a2c_" + args.name)

    net = common.AtariA2C(envs[0].observation_space.shape, envs[0].action_space.n)
    if args.cuda:
        net.cuda()
    print(net)
    optimizer = optim.RMSprop(net.parameters(), lr=LEARNING_RATE, eps=1e-5)

    step_idx = 0
    with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
        for mb_obs, mb_rewards, mb_actions, mb_values in iterate_train_batches(envs, net, cuda=args.cuda):
            optimizer.zero_grad()
            mb_adv = mb_rewards - mb_values
            adv_v = Variable(torch.from_numpy(mb_adv))
            obs_v = Variable(torch.from_numpy(mb_obs))
            rewards_v = Variable(torch.from_numpy(mb_rewards))
            actions_t = torch.LongTensor(mb_actions.tolist())
            if args.cuda:
                adv_v = adv_v.cuda()
                obs_v = obs_v.cuda()
                rewards_v = rewards_v.cuda()
                actions_t = actions_t.cuda()
            logits_v, values_v = net(obs_v)
            log_prob_v = F.log_softmax(logits_v)
            log_prob_actions_v = adv_v * log_prob_v[range(len(mb_actions)), actions_t]

            loss_policy_v = -log_prob_actions_v.mean()
            loss_value_v = F.mse_loss(values_v, rewards_v)

            prob_v = F.softmax(logits_v)
            entropy_loss_v = (prob_v * log_prob_v).sum(dim=1).mean()
            loss_v = common.ENTROPY_BETA * entropy_loss_v + common.VALUE_LOSS_COEF * loss_value_v + loss_policy_v
            loss_v.backward()
            nn_utils.clip_grad_norm(net.parameters(), common.CLIP_GRAD)
            optimizer.step()

            tb_tracker.track("advantage", mb_adv, step_idx)
            tb_tracker.track("values", values_v, step_idx)
            tb_tracker.track("batch_rewards", rewards_v, step_idx)
            tb_tracker.track("loss_entropy", entropy_loss_v, step_idx)
            tb_tracker.track("loss_policy", loss_policy_v, step_idx)
            tb_tracker.track("loss_value", loss_value_v, step_idx)
            tb_tracker.track("loss_total", loss_v, step_idx)

            step_idx += 1
