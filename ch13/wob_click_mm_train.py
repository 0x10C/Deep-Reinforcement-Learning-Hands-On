#!/usr/bin/env python3
import os
import gym
import universe
import argparse
import numpy as np
from tensorboardX import SummaryWriter

from lib import wob_vnc, model_vnc, common

import ptan

import torch
import torch.nn.utils as nn_utils
import torch.nn.functional as F
import torch.optim as optim


REMOTES_HOST = "gpu"
REMOTES_COUNT = 4
ENV_NAME = "wob.mini.ClickButton-v0"

GAMMA = 0.99
REWARD_STEPS = 2
BATCH_SIZE = 16
LEARNING_RATE = 0.001
ENTROPY_BETA = 0.001
CLIP_GRAD = 0.1

SAVES_DIR = "saves"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    parser.add_argument("--cuda", default=False, action='store_true', help="CUDA mode")
    parser.add_argument("--port-ofs", type=int, default=0, help="Offset for container's ports, default=0")
    parser.add_argument("--env", default=ENV_NAME, help="Environment name to solve, default=" + ENV_NAME)
    args = parser.parse_args()

    env_name = args.env
    if not env_name.startswith('wob.mini.'):
        env_name = "wob.mini." + env_name

    name = env_name.split('.')[-1] + "_" + args.name
    writer = SummaryWriter(comment="-wob_click_mm_" + name)
    saves_path = os.path.join(SAVES_DIR, name)
    os.makedirs(saves_path, exist_ok=True)

    env = gym.make(env_name)
    env = universe.wrappers.experimental.SoftmaxClickMouse(env)
    env = wob_vnc.MiniWoBCropper(env, keep_text=True)
    wob_vnc.configure(env, wob_vnc.remotes_url(port_ofs=args.port_ofs, hostname=REMOTES_HOST, count=REMOTES_COUNT))

    net = model_vnc.ModelMultimodal(input_shape=wob_vnc.WOB_SHAPE, n_actions=env.action_space.n)
    if args.cuda:
        net.cuda()
    print(net)
    optimizer = optim.Adam(net.parameters(), lr=LEARNING_RATE, eps=1e-3)

    preprocessor = model_vnc.MultimodalPreprocessor()
    agent = ptan.agent.PolicyAgent(lambda x: net(x)[0], cuda=args.cuda,
                                   apply_softmax=True, preprocessor=preprocessor)
    exp_source = ptan.experience.ExperienceSourceFirstLast(
        [env], agent, gamma=GAMMA, steps_count=REWARD_STEPS, vectorized=True)

    best_reward = None
    with common.RewardTracker(writer) as tracker:
        with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
            batch = []
            for step_idx, exp in enumerate(exp_source):
                rewards_steps = exp_source.pop_rewards_steps()
                if rewards_steps:
                    rewards, steps = zip(*rewards_steps)
                    tb_tracker.track("episode_steps", np.mean(steps), step_idx)

                    mean_reward = tracker.reward(np.mean(rewards), step_idx)
                    if mean_reward is not None:
                        if best_reward is None or mean_reward > best_reward:
                            if best_reward is not None:
                                name = "best_%.3f_%d.dat" % (mean_reward, step_idx)
                                fname = os.path.join(saves_path, name)
                                torch.save(net.state_dict(), fname)
                                print("Best reward updated: %.3f -> %.3f" % (best_reward, mean_reward))
                            best_reward = mean_reward
                batch.append(exp)
                if len(batch) < BATCH_SIZE:
                    continue

                states_v, actions_t, vals_ref_v = \
                    common.unpack_batch(batch, net, last_val_gamma=GAMMA ** REWARD_STEPS,
                                        cuda=args.cuda)
                batch.clear()

                optimizer.zero_grad()
                logits_v, value_v = net(states_v)

                loss_value_v = F.mse_loss(value_v, vals_ref_v)

                log_prob_v = F.log_softmax(logits_v)
                adv_v = vals_ref_v - value_v.detach()
                log_prob_actions_v = adv_v * log_prob_v[range(BATCH_SIZE), actions_t]
                loss_policy_v = -log_prob_actions_v.mean()

                prob_v = F.softmax(logits_v)
                entropy_loss_v = ENTROPY_BETA * (prob_v * log_prob_v).sum(dim=1).mean()

                loss_v = entropy_loss_v + loss_value_v + loss_policy_v
                loss_v.backward()
                nn_utils.clip_grad_norm(net.parameters(), CLIP_GRAD)
                optimizer.step()

                tb_tracker.track("advantage", adv_v, step_idx)
                tb_tracker.track("values", value_v, step_idx)
                tb_tracker.track("batch_rewards", vals_ref_v, step_idx)
                tb_tracker.track("loss_entropy", entropy_loss_v, step_idx)
                tb_tracker.track("loss_policy", loss_policy_v, step_idx)
                tb_tracker.track("loss_value", loss_value_v, step_idx)
                tb_tracker.track("loss_total", loss_v, step_idx)

    pass
