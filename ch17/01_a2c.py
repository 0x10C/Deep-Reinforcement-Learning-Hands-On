#!/usr/bin/env python3
import os
import ptan
import argparse
from tensorboardX import SummaryWriter

import torch
import torch.optim as optim

from lib import common


LEARNING_RATE = 7e-4
TEST_EVERY_BATCH = 100


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    saves_path = os.path.join("saves", "01_a2c_" + args.name)
    os.makedirs(saves_path, exist_ok=True)

    envs = [common.make_env() for _ in range(common.NUM_ENVS)]
    test_env = common.make_env(test=True)
    writer = SummaryWriter(comment="-01_a2c_" + args.name)

    net = common.AtariA2C(envs[0].observation_space.shape, envs[0].action_space.n)
    if args.cuda:
        net.cuda()
    print(net)
    optimizer = optim.RMSprop(net.parameters(), lr=LEARNING_RATE, eps=1e-5)

    step_idx = 0
    best_reward = None
    best_test_reward = None
    with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
        for mb_obs, mb_rewards, mb_actions, mb_values, done_rewards, done_steps in common.iterate_batches(envs, net, cuda=args.cuda):
            if len(done_rewards) > 0:
                if best_reward is None:
                    best_reward = done_rewards.max()
                elif best_reward < done_rewards.max():
                    best_reward = done_rewards.max()
                tb_tracker.track("total_reward_max", best_reward, step_idx)
                tb_tracker.track("total_reward", done_rewards, step_idx)
                tb_tracker.track("total_steps", done_steps, step_idx)
                print("%d: done %d episodes, mean_reward=%.2f, best_reward=%.2f" % (
                    step_idx, len(done_rewards), done_rewards.mean(), best_reward))

            common.train_a2c(net, mb_obs, mb_rewards, mb_actions, mb_values,
                             optimizer, tb_tracker, step_idx, cuda=args.cuda)
            step_idx += 1

            if step_idx % TEST_EVERY_BATCH == 0:
                test_reward, test_steps = common.test_model(test_env, net, cuda=args.cuda)
                tb_tracker.track("test_reward", test_reward, step_idx)
                tb_tracker.track("test_steps", test_steps, step_idx)
                if best_test_reward is None or best_test_reward < test_reward:
                    if best_test_reward is not None:
                        fname = os.path.join(saves_path, "best_%08.3f_%d.dat" % (test_reward, step_idx))
                        torch.save(net.state_dict(), fname)
                    best_test_reward = test_reward
                print("%d: test reward=%.2f, steps=%.2f, best_reward=%.2f" % (
                    step_idx, test_reward, test_steps, best_test_reward))
