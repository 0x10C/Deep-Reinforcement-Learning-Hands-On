import ptan
import numpy as np

import torch
import torch.nn as nn

HID_SIZE = 128


class ModelA2C(nn.Module):
    def __init__(self, obs_size, act_size):
        super(ModelA2C, self).__init__()

        self.base = nn.Sequential(
            nn.Linear(obs_size, HID_SIZE),
            nn.ReLU(),
        )
        self.mu = nn.Sequential(
            nn.Linear(HID_SIZE, act_size),
            nn.Tanh(),
        )
        self.var = nn.Sequential(
            nn.Linear(HID_SIZE, act_size),
            nn.Softplus(),
        )
        self.value = nn.Linear(HID_SIZE, 1)

    def forward(self, x):
        base_out = self.base(x)
        return self.mu(base_out), self.var(base_out), self.value(base_out)


class ModelDDPG(nn.Module):
    def __init__(self, obs_size, act_size):
        super(ModelDDPG, self).__init__()

        self.n_actor = nn.Sequential(
            nn.Linear(obs_size, 300),
            nn.ReLU(),
            nn.Linear(300, 200),
            nn.ReLU(),
            nn.Linear(200, act_size),
            nn.Tanh()
        )

        self.n_critic = nn.Sequential(
            nn.Linear(obs_size + act_size, 400),
            nn.ReLU(),
            nn.Linear(400, 300),
            nn.ReLU(),
            nn.Linear(300, 1)
        )

    def actor(self, x):
        return self.n_actor(x)

    def critic(self, obs, act):
        critic_input = torch.cat((obs, act), dim=1)
        return self.n_critic(critic_input)

    def forward(self, x):
        action = self.actor(x)
        return action, self.critic(x, action)


class AgentA2C(ptan.agent.BaseAgent):
    def __init__(self, net, cuda=False):
        self.net = net
        self.cuda = cuda

    def __call__(self, states, agent_states):
        states_v = ptan.agent.float32_preprocessor(states, cuda=self.cuda)

        mu_v, var_v, _ = self.net(states_v)
        mu = mu_v.data.cpu().numpy()
        sigma = torch.sqrt(var_v).data.cpu().numpy()
        actions = np.random.normal(mu, sigma)
        actions = np.clip(actions, -1, 1)
        return actions, agent_states


class AgentDDPG(ptan.agent.BaseAgent):
    """
    Agent implementing Orstein-Uhlenbeck exploration process
    """
    def __init__(self, net, cuda=False, ou_enabled=True, ou_mu=0.0, ou_teta=0.15, ou_sigma=0.2, ou_epsilon=1.0):
        self.net = net
        self.cuda = cuda
        self.ou_enabled = ou_enabled
        self.ou_mu = ou_mu
        self.ou_teta = ou_teta
        self.ou_sigma = ou_sigma
        self.ou_epsilon = ou_epsilon

    def initial_state(self):
        return None

    def __call__(self, states, agent_states):
        states_v = ptan.agent.float32_preprocessor(states, cuda=self.cuda)
        mu_v = self.net.actor(states_v)
        actions = mu_v.data.cpu().numpy()

        if self.ou_enabled and self.ou_epsilon > 0:
            new_a_states = []
            for a_state, action in zip(agent_states, actions):
                if a_state is not None:
                    noise = self.ou_teta * (self.ou_mu - action)
                    noise += self.ou_sigma * np.random.normal(size=action.shape)
                    action += self.ou_epsilon * noise
                new_a_states.append(action)
        else:
            new_a_states = agent_states

        actions = np.clip(actions, -1, 1)
        return actions, new_a_states

pass
