#!/usr/bin/env python3
import argparse
import gym
import gym.spaces
import copy
import time
import numpy as np
import collections

from PIL import Image

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable

from tensorboardX import SummaryWriter

from collections import deque

ENV_NAME = "PongNoFrameskip-v4"
GAMMA = 0.99
BATCH_SIZE = 32
REPLAY_SIZE = 10000
LEARNING_RATE = 1e-4
SYNC_TARGET_FRAMES = 1000
REPLAY_START_SIZE = 10000

EPSILON_DECAY_LAST_FRAME = 10**5
EPSILON_START = 1.0
EPSILON_FINAL = 0.02

SUMMARY_EVERY_FRAME = 100


class ImageWrapper(gym.ObservationWrapper):
    TARGET_SIZE = 84

    def __init__(self, env):
        super(ImageWrapper, self).__init__(env)
        probe = np.zeros_like(env.observation_space.low, np.uint8)
        self.observation_space = gym.spaces.Box(0, 255, self._observation(probe).shape)

    def _observation(self, obs):
        img = Image.fromarray(obs)
        img = img.convert("YCbCr")
        img = img.resize((self.TARGET_SIZE, self.TARGET_SIZE))
        data = np.asarray(img.getdata(0), np.uint8).reshape(img.size)
        return np.expand_dims(data, 0)


class BufferWrapper(gym.ObservationWrapper):
    def __init__(self, env, n_steps, dtype=np.uint8):
        super(BufferWrapper, self).__init__(env)
        self.dtype = dtype
        old_space = env.observation_space
        self.observation_space = gym.spaces.Box(old_space.low.repeat(n_steps, axis=0),
                                                old_space.high.repeat(n_steps, axis=0))

    def _reset(self):
        self.buffer = np.zeros_like(self.observation_space.low, dtype=self.dtype)
        return self._observation(self.env.reset())

    def _observation(self, observation):
        self.buffer[:-1] = self.buffer[1:]
        self.buffer[-1] = observation
        return self.buffer


class FireResetEnv(gym.Wrapper):
    def __init__(self, env=None):
        """For environments where the user need to press FIRE for the game to start."""
        super(FireResetEnv, self).__init__(env)
        assert env.unwrapped.get_action_meanings()[1] == 'FIRE'
        assert len(env.unwrapped.get_action_meanings()) >= 3

    def _reset(self):
        self.env.reset()
        obs, _, done, _ = self.env.step(1)
        if done:
            self.env.reset()
        obs, _, done, _ = self.env.step(2)
        if done:
            self.env.reset()
        return obs


class MaxAndSkipEnv(gym.Wrapper):
    def __init__(self, env=None, skip=4):
        """Return only every `skip`-th frame"""
        super(MaxAndSkipEnv, self).__init__(env)
        # most recent raw observations (for max pooling across time steps)
        self._obs_buffer = deque(maxlen=2)
        self._skip = skip

    def _step(self, action):
        total_reward = 0.0
        done = None
        for _ in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            self._obs_buffer.append(obs)
            total_reward += reward
            if done:
                break

        max_frame = np.max(np.stack(self._obs_buffer), axis=0)

        return max_frame, total_reward, done, info

    def _reset(self):
        """Clear past frame buffer and init. to first obs. from inner env."""
        self._obs_buffer.clear()
        obs = self.env.reset()
        self._obs_buffer.append(obs)
        return obs


class ScaledFloatFrame(gym.ObservationWrapper):
    def _observation(self, obs):
        # careful! This undoes the memory optimization, use
        # with smaller replay buffers only.
        return np.array(obs).astype(np.float32) / 255.0


def make_env():
    env = gym.make(ENV_NAME)
    env = FireResetEnv(env)
    env = MaxAndSkipEnv(env)
    env = ImageWrapper(env)
    env = ScaledFloatFrame(env)
    env = BufferWrapper(env, 4, dtype=np.float32)
    return env


class DQN(nn.Module):
    def __init__(self, input_shape, n_actions):
        super(DQN, self).__init__()

        self.conv = nn.Sequential(
            nn.Conv2d(input_shape[0], 32, kernel_size=8, stride=4),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 64, kernel_size=3, stride=1),
            nn.ReLU()
        )

        conv_out_size = self._get_conv_out(input_shape)
        self.fc = nn.Sequential(
            nn.Linear(conv_out_size, 512),
            nn.ReLU(),
            nn.Linear(512, n_actions)
        )

    def _get_conv_out(self, shape):
        o = self.conv(Variable(torch.zeros(1, *shape)))
        return int(np.prod(o.size()))

    def forward(self, x):
        conv_out = self.conv(x).view(x.size()[0], -1)
        return self.fc(conv_out)


Experience = collections.namedtuple('Experience', field_names=['state', 'action', 'reward', 'done', 'new_state'])


class ExperienceBuffer:
    def __init__(self, capacity):
        self.capacity = capacity
        self.buffer = collections.deque()

    def __len__(self):
        return len(self.buffer)

    def append(self, experience):
        self.buffer.append(experience)
        while len(self.buffer) > self.capacity:
            self.buffer.popleft()

    def sample(self, batch_size):
        indices = np.random.choice(len(self.buffer), batch_size, replace=False)
        return [self.buffer[idx] for idx in indices]


class Agent:
    def __init__(self, env, exp_buffer):
        self.env = env
        self.exp_buffer = exp_buffer
        self._reset()

    def _reset(self):
        self.state = env.reset()
        self.total_reward = 0.0

    def play_step(self, net, epsilon=0.0, cuda=False):
        done_reward = None

        if np.random.random() < epsilon:
            action = env.action_space.sample()
        else:
            state_v = Variable(torch.from_numpy(np.array([self.state], copy=False)))
            if cuda:
                state_v = state_v.cuda()
            q_vals_v = net(state_v)
            _, act_v = torch.max(q_vals_v, dim=1)
            action = int(act_v.data.cpu().numpy()[0])

        # do step in the environment
        new_state, reward, is_done, _ = self.env.step(action)
        self.total_reward += reward
        new_state = new_state

        self.exp_buffer.append(Experience(self.state, action, reward, is_done, new_state))
        self.state = new_state
        if is_done:
            done_reward = self.total_reward
            self._reset()
        return done_reward


def calc_loss(batch, net, target_net, cuda=False):
    states, actions, rewards, dones, next_states = zip(*batch)
    states_v = Variable(torch.from_numpy(np.array(states, copy=False)))
    next_states_v = Variable(torch.from_numpy(np.array(next_states, copy=False)), volatile=True)
    actions_v = Variable(torch.LongTensor(actions))
    rewards_v = Variable(torch.FloatTensor(rewards))
    done_mask_t = torch.ByteTensor(dones)

    if cuda:
        states_v = states_v.cuda()
        next_states_v = next_states_v.cuda()
        actions_v = actions_v.cuda()
        rewards_v = rewards_v.cuda()
        done_mask_t = done_mask_t.cuda()

    state_action_values_v = net(states_v).gather(1, actions_v.unsqueeze(-1)).squeeze(-1)
    next_state_values_v = target_net(next_states_v).max(1)[0]
    next_state_values_v[done_mask_t] = 0.0
    next_state_values_v.volatile = False

    bellman_q_v = rewards_v + next_state_values_v * GAMMA
    loss_v = nn.MSELoss()(state_action_values_v, bellman_q_v)
    return loss_v


def play_episode(env, net, cuda=False):
    state = env.reset()
    total_reward = 0.0

    while True:
        state_v = Variable(torch.from_numpy(np.array([state], copy=False)))
        if cuda:
            state_v = state_v.cuda()
        q_vals_v = net(state_v)
        _, act_v = torch.max(q_vals_v, dim=1)
        action = act_v.data.cpu().numpy()[0]
        new_state, reward, is_done, _ = env.step(action)
        total_reward += reward
        if is_done:
            break
        state = new_state
    return total_reward


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action='store_true', help="Enable cuda mode")
    args = parser.parse_args()

    writer = SummaryWriter(comment='-pong-fast')
    env = make_env()
    test_env = make_env()

    net = DQN(env.observation_space.shape, env.action_space.n)
    tgt_net = DQN(env.observation_space.shape, env.action_space.n)
    print(net)

    exp_buffer = ExperienceBuffer(capacity=REPLAY_SIZE)
    agent = Agent(env, exp_buffer)

    optimizer = optim.RMSprop(net.parameters(), lr=LEARNING_RATE, momentum=0.95)
    if args.cuda:
        net.cuda()
        tgt_net.cuda()

    epsilon = EPSILON_START
    episode_rewards = []
    frame_idx = 0
    last_ts = time.time()
    last_frame = 0

    while True:
        frame_idx += 1
        epsilon = max(EPSILON_FINAL, EPSILON_START - frame_idx / EPSILON_DECAY_LAST_FRAME)
        reward = agent.play_step(tgt_net, epsilon=epsilon, cuda=args.cuda)
        if reward is not None:
            episode_rewards.append(reward)
            speed = (frame_idx - last_frame) / (time.time() - last_ts)
            last_ts = time.time()
            last_frame = frame_idx
            mean_100 = np.mean(episode_rewards[-100:])
            print("%d: reward %.1f, mean rewards %.2f, episodes %d, speed %.2f frames/sec, epsilon %.2f" % (
                frame_idx, reward, mean_100, len(episode_rewards), speed, epsilon))
            writer.add_scalar("reward", reward, frame_idx)
            writer.add_scalar("speed", speed, frame_idx)
            writer.add_scalar("epsilon", epsilon, frame_idx)
            writer.add_scalar("reward_100", mean_100, frame_idx)

        if frame_idx < REPLAY_START_SIZE:
            continue

        batch = exp_buffer.sample(BATCH_SIZE)
        optimizer.zero_grad()
        loss_v = calc_loss(batch, net, tgt_net, cuda=args.cuda)
        loss_v.backward()
        optimizer.step()

        if frame_idx % SYNC_TARGET_FRAMES == 0:
            tgt_net.load_state_dict(net.state_dict())
            reward = play_episode(test_env, tgt_net, cuda=args.cuda)
            writer.add_scalar("reward_test", reward, frame_idx)
            print("%d: synced, test episode reward=%.1f" % (frame_idx, reward))
