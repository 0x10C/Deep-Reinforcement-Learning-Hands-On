#!/usr/bin/env python3
import os
import time
import ptan
import random
import argparse
import collections
import numpy as np

from lib import game, model, mcts

from tensorboardX import SummaryWriter

import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable


MCTS_SEARCHES = 5
MCTS_BATCH_SIZE = 8
REPLAY_BUFFER = 10000
LEARNING_RATE = 1e-4
BATCH_SIZE = 128
MIN_REPLAY_TO_TRAIN = 200

BEST_SCORES_HIST = 30
BEST_NET_WIN_RATIO = 0.6


def play_game(replay_buffer, net1, net2, cuda=False):
    """
    Play one single game, memorizing transitions into the replay buffer
    :param replay_buffer: queue with (state, probs, values), if None, nothing is stored
    :param net1: player1
    :param net2: player2
    :return: value for the game in respect to player1 (+1 if p1 won, -1 if lost, 0 if draw)
    """
    assert isinstance(replay_buffer, (collections.deque, type(None)))
    mcts_store = mcts.MCTS()
    state = game.INITIAL_STATE
    nets = [net1, net2]
    cur_player = np.random.choice(2)
    step = 0
    result = None
    while result is None:
        mcts_store.search_batch(MCTS_SEARCHES, MCTS_BATCH_SIZE, state, cur_player, nets[cur_player], cuda=cuda)
        probs, values = mcts_store.get_policy_value(state)
        if replay_buffer is not None:
            replay_buffer.append((state, cur_player, probs, values))
        action = np.random.choice(game.GAME_COLS, p=probs)
        if action not in game.possible_moves(state):
            print("Impossible action selected")
        state, won = game.move(state, action, cur_player)
        if won:
            result = 1.0 if cur_player == 0 else -1
        cur_player = 1-cur_player
        # check the draw case
        if len(game.possible_moves(state)) == 0:
            result = 0.0
        step += 1
    return result, step, len(mcts_store)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable CUDA")
    args = parser.parse_args()

    saves_path = os.path.join("saves", args.name)
    os.makedirs(saves_path, exist_ok=True)
    writer = SummaryWriter(comment="-" + args.name)

    net = model.Net(input_shape=model.OBS_SHAPE, actions_n=game.GAME_COLS)
    if args.cuda:
        net.cuda()
    best_net = ptan.agent.TargetNet(net)
    print(net)

    optimizer = optim.SGD(net.parameters(), lr=LEARNING_RATE, momentum=0.9)

    replay_buffer = collections.deque(maxlen=REPLAY_BUFFER)
    cur_net_scores = collections.deque(maxlen=BEST_SCORES_HIST)
    step_idx = 0
    best_idx = 0

    with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
        while True:
            step_idx += 1
            t = time.time()
            game_res, game_steps, game_nodes = play_game(replay_buffer, net, best_net.target_model, cuda=args.cuda)
            dt = time.time() - t
            cur_net_scores.append(game_res)
            if len(cur_net_scores) < BEST_SCORES_HIST:
                mean_score = -np.inf
            else:
                mean_score = np.mean(cur_net_scores)
                tb_tracker.track("score_mean", mean_score, step_idx)
            speed_steps = game_steps / dt
            speed_nodes = game_nodes / dt
            tb_tracker.track("speed_steps", speed_steps, step_idx)
            tb_tracker.track("speed_nodes", speed_nodes, step_idx)
            print("Game %d, steps %3d, score %4.1f, mean %5.2f, steps/s %5.2f, nodes/s %6.2f" % (
                step_idx, game_steps, game_res, mean_score, speed_steps, speed_nodes))

            if len(replay_buffer) < MIN_REPLAY_TO_TRAIN:
                continue

            # train
            batch = random.sample(replay_buffer, BATCH_SIZE)
            batch_states, batch_who_moves, batch_probs, batch_values = zip(*batch)
            batch_states_lists = [game.decode_binary(state) for state in batch_states]
            states_v = model.state_lists_to_batch(batch_states_lists, batch_who_moves, args.cuda)

            optimizer.zero_grad()
            probs_v = Variable(torch.FloatTensor(batch_probs))
            values_v = Variable(torch.FloatTensor(batch_values))
            if args.cuda:
                probs_v = probs_v.cuda()
                values_v = values_v.cuda()
            # obtain expected value for the state
            state_value_v = (probs_v * values_v).sum(dim=1).detach()

            out_logits_v, out_values_v = net(states_v)

            loss_value_v = F.mse_loss(out_values_v, state_value_v)
            loss_policy_v = -F.log_softmax(out_logits_v) * probs_v
            loss_policy_v = loss_policy_v.sum(dim=1).mean()

            loss_v = loss_policy_v + loss_value_v
            loss_v.backward()
            optimizer.step()

            tb_tracker.track("loss_total", loss_v, step_idx)
            tb_tracker.track("loss_value", loss_value_v, step_idx)
            tb_tracker.track("loss_policy", loss_policy_v, step_idx)

            # check the reward ratio of the network
            if np.mean(cur_net_scores) > BEST_NET_WIN_RATIO:
                print("Net is better than cur best, sync")
                best_net.sync()
                cur_net_scores.clear()
                best_idx += 1
                file_name = os.path.join(saves_path, "best_%03d_%05d.dat" % (best_idx, step_idx))
                torch.save(net.state_dict(), file_name)
