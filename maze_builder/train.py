# TODO:
# - try using L1Linear in policy network, to avoid premature large outputs which can block off exploration
#   - or try clamping the output of the network as-is
#   - or try using different scale instead of logits, for fatter tails than exponential
# - try restructuring training into epochs each of which consist of 3 phases:
#   1. Generate experience data using fixed policy
#   2. Sample from accumulated experience to improve the value network
#      - Retain some percentage of experiences from previous epochs
#         - Ideally, do this selectively based on the magnitude of their last training error. But if so, weight
#           them appropriately.
#      - Don't touch the policy network in this step. The idea is to get an accurate-enough estimate of the value
#        function first, to avoid instability or premature shutting off of exploration; also we wouldn't want to
#        use old experiences to update the policy.
#      - Depending on the hyperparameters (batch size and number of batches per phase) it may happen that the same
#        experiences are sampled many times in this phase. This may or may not be necessary but would improve the
#        accuracy of the value network more.
#   3. Update the policy function using only the new experience data generated in part 1 of the current phase.
# - try again sharing a subnetwork between value and policy networks, but be sure to let only the policy network
#   drive its updates.
# - try TD(lambda)
import gym
import numpy as np
import torch
import collections
import logging
import math
from maze_builder.env import MazeBuilderEnv
import maze_builder.crateria
from maze_builder.model_components import L1LinearScaled

logging.basicConfig(format='%(asctime)s %(message)s',
                    level=logging.INFO,
                    handlers=[logging.FileHandler("train.log"),
                              logging.StreamHandler()])
torch.autograd.set_detect_anomaly(True)

class MinOut(torch.nn.Module):
    def __init__(self, arity):
        super().__init__()
        self.arity = arity

    def forward(self, X):
        self.X = X
        X = X.view([X.shape[0], self.arity, -1])
        out = torch.min(X, dim=1)[0]
        self.out = out
        return out

    def penalty(self):
        return 0.0


class PReLU:
    def __init__(self, num_inputs, dtype=torch.float32, device=None):
        super().__init__()
        self.num_inputs = num_inputs
        self.slope_left = torch.nn.Parameter(torch.zeros([num_inputs], dtype=dtype, device=device))
        self.slope_right = torch.nn.Parameter(torch.ones([num_inputs], dtype=dtype, device=device))

    def forward(self, X):
        out = self.slope_right * torch.clamp(X, min=0.0) + self.slope_left * torch.clamp(X, max=0.0)
        return out


class MainNetwork(torch.nn.Module):
    def __init__(self, widths):
        super().__init__()
        self.depth = len(widths) - 1
        self.lin_layers = torch.nn.ModuleList([])
        self.act_layers = torch.nn.ModuleList([])
        arity = 1
        for i in range(self.depth):
            if i != self.depth - 1:
                self.lin_layers.append(torch.nn.Linear(widths[i], widths[i + 1] * arity))
                # self.lin_layers.append(L1LinearScaled(widths[i], widths[i + 1] * arity))
                # self.act_layers.append(MinOut(arity))
                self.act_layers.append(torch.nn.ReLU())
                # self.act_layers.append(torch.nn.PReLU(widths[i + 1]))
            else:
                self.lin_layers.append(torch.nn.Linear(widths[i], widths[i + 1]))
                # self.lin_layers.append(L1LinearScaled(widths[i], widths[i + 1]))

    def forward(self, X):
        for i in range(self.depth):
            X = self.lin_layers[i](X)
            if i != self.depth - 1:
                X = self.act_layers[i](X)
        return X

    def decay(self, decay):
        if decay == 0.0:
            return
        for mod in self.modules():
            if isinstance(mod, torch.nn.Linear):
                mod.weight.data *= 1 - decay

    # def project(self):
    #     for layer in self.lin_layers:
    #         layer.project()


class Model(torch.nn.Module):
    def __init__(self, widths, num_actions):
        super().__init__()
        # self.main_network = torch.nn.Sequential(MainNetwork(widths), torch.nn.ReLU())
        # self.policy_layer = torch.nn.Linear(widths[-1], num_actions)
        # self.value_layer = torch.nn.Linear(widths[-1], 1)
        self.main_network = torch.nn.Sequential()
        self.policy_layer = MainNetwork(widths + [num_actions])
        self.value_layer = MainNetwork(widths + [1])

    def weight_decay(self, decay):
        for mod in self.modules():
            if isinstance(mod, torch.nn.Linear):
                mod.weight.data *= 1 - decay

    def forward_full(self, X):
        main = self.main_network(X)
        p_raw = self.policy_layer(main)
        value = self.value_layer(main)[:, 0]
        return p_raw, value

    def forward_policy(self, X):
        main = self.main_network(X)
        p_raw = self.policy_layer(main)
        return p_raw

    def forward_value(self, X):
        main = self.main_network(X)
        value = self.value_layer(main)[:, 0]
        return value


class ReplayBuffer():
    def __init__(self, capacity, observation_size, reward_horizon):
        self.size = 0
        self.capacity = capacity
        self.reward_horizon = reward_horizon
        self.deque = collections.deque(maxlen=reward_horizon)
        self.deque_total_reward = 0.0
        self.state1 = torch.empty([capacity, observation_size], dtype=torch.float32)
        self.state2 = torch.empty([capacity, observation_size], dtype=torch.float32)
        self.action = torch.empty(capacity, dtype=torch.int64)
        self.mean_reward = torch.empty(capacity, dtype=torch.float32)

    def append(self, state1: np.array, state2: np.array, action: int, reward: float, done: bool, artificial_end: bool):
        if artificial_end and not done:
            # Don't use current data in deque: total rewards would be invalid since game was artificially ended
            self.deque.clear()
            self.deque_total_reward = 0.0
            return
        assert len(self.deque) < self.reward_horizon
        self.deque.append((state1, state2, action, reward))
        self.deque_total_reward += reward
        if len(self.deque) == self.reward_horizon:
            self._process_oldest()
        if done:
            while len(self.deque) > 0:
                self._process_oldest()

    def _process_oldest(self):
        # Process the oldest element of the deque
        state1, state2, action, reward = self.deque.popleft()
        self._append(state1, state2, action, self.deque_total_reward / self.reward_horizon)
        self.deque_total_reward -= reward

    def _append(self, state1: np.array, state2: np.array, action: int, mean_reward: float):
        if self.size == self.capacity:
            self.downsize()
        self.state1[self.size, :] = torch.from_numpy(state1)
        self.state2[self.size, :] = torch.from_numpy(state2)
        self.action[self.size] = action
        self.mean_reward[self.size] = mean_reward
        self.size += 1

    def downsize(self):
        # Keep the most recent half of observations.
        start = self.size // 2
        end = self.capacity
        size = end - start
        self.state1[:size, :] = self.state1[start:end, :]
        self.state2[:size, :] = self.state2[start:end, :]
        self.action[:size] = self.action[start:end]
        self.mean_reward[:size] = self.mean_reward[start:end]
        self.size = size

    def sample(self, sample_size):
        ind = np.random.choice(self.size, sample_size, replace=False)
        state1 = self.state1[ind, :]
        state2 = self.state2[ind, :]
        action = self.action[ind]
        total_reward = self.mean_reward[ind]
        return state1, state2, action, total_reward



def softer_max(X, dim):
    # These two branches are for numerical stability (they are mathematically equivalent to each other):
    Y = torch.where(X > 0,
                    torch.sqrt(1 + X ** 2) + X,
                    1 / (torch.sqrt(1 + X ** 2) - X))
    return Y / torch.sum(Y, dim=dim, keepdim=True)

class TrainingSession():
    def __init__(self, env: gym.Env,
                 value_network: torch.nn.Module,
                 policy_network: torch.nn.Module,
                 value_optimizer: torch.optim.Optimizer,
                 policy_optimizer: torch.optim.Optimizer,
                 weight_decay: float = 0.0
                 ):
        self.env = env
        self.value_network = value_network
        self.policy_network = policy_network
        self.value_optimizer = value_optimizer
        self.policy_optimizer = policy_optimizer
        self.weight_decay = weight_decay
        self.state_width = np.prod(env.observation_space.shape)
        self.round_number = 0

    def generate_data(self, num_episodes: int, episode_length: int):
        state0 = torch.empty([num_episodes, episode_length, self.state_width], dtype=torch.float32)
        action = torch.empty([num_episodes, episode_length], dtype=torch.int64)
        reward = torch.empty([num_episodes, episode_length], dtype=torch.float32)
        state1 = torch.empty([num_episodes, episode_length, self.state_width], dtype=torch.float32)
        for e in range(num_episodes):
            X0 = torch.from_numpy(env.reset()).view(1, -1).to(torch.float32)
            for i in range(episode_length):
                with torch.no_grad():
                    p_raw = self.policy_network(X0)
                dist = torch.distributions.Categorical(logits=p_raw[0, :])
                a = dist.sample().item()
                observation, r, _, _ = env.step(a)
                X1 = torch.from_numpy(observation).view(1, -1).to(torch.float32)
                state0[e, i, :] = X0
                reward[e, i] = r
                action[e, i] = a
                state1[e, i, :] = X1
        return state0, action, reward, state1

    def train_value(self, state0, action, reward, state1, batch_size: int, horizon: int, eval: bool):
        assert len(state0.shape) == 2
        num_rows = state0.shape[0]
        num_batches = num_rows // batch_size  # Round down, to discard any remaining partial batch

        total_loss = 0.0
        total_mean_value = 0.0
        for j in range(num_batches):
            start = j * batch_size
            end = (j + 1) * batch_size

            state0_batch = state0[start:end, :]
            # reward_batch = reward[start:end]
            # state1_batch = state1[start:end, :]

            if eval:
                with torch.no_grad():
                    value0_batch = self.value_network(state0_batch)[:, 0]
            else:
                value0_batch = self.value_network(state0_batch)[:, 0]
            # with torch.no_grad():
            #     value1_batch = self.value_network(state1_batch)
            # target_batch = (1 - gamma) * reward_batch + gamma * value1_batch
            # target_batch = target[start:end]
            target_batch = reward[start:end]
            loss = torch.mean((value0_batch - target_batch) ** 2)

            if not eval:
                self.value_optimizer.zero_grad()
                loss.backward()
                self.value_optimizer.step()
                lr = self.value_optimizer.param_groups[0]['lr']
                self.value_network.decay(self.weight_decay * lr)
            total_loss += loss.item()
            total_mean_value += torch.mean(value0_batch).item()
        return total_loss / num_batches, total_mean_value / num_batches

    def flatten_data(self, state0, action, reward, state1, horizon: int):
        # Reshape the data to collapse the episode and step dimensions into one, and compute rolling mean rewards
        cumul_reward = torch.cumsum(reward, dim=1)
        horizon_reward = (cumul_reward[:, horizon:] - cumul_reward[:, :-horizon]) / horizon
        num_episodes = state0.shape[0]
        episode_length = state0.shape[1]
        num_rows = num_episodes * (episode_length - horizon)
        state0 = state0[:, :-horizon, :].reshape(num_rows, self.state_width)
        action = action[:, :-horizon].reshape(num_rows)
        horizon_reward = horizon_reward.reshape(num_rows)
        state1 = state1[:, :-horizon].reshape(num_rows, self.state_width)
        return state0, action, horizon_reward, state1

    def shuffle_flat_data(self, state0, action, reward, state1):
        num_rows = state0.shape[0]
        perm = torch.randperm(num_rows)
        state0 = state0[perm, :]
        action = action[perm]
        reward = reward[perm]
        state1 = state1[perm, :]
        return state0, action, reward, state1

    def train_round(self, num_episodes: int, episode_length: int, num_passes, batch_size: int, horizon: int):
        # Generate sample data using current policy
        train_state0, train_action, train_reward, train_state1 = self.generate_data(
            num_episodes=num_episodes,
            episode_length=episode_length)
        train_state0, train_action, train_horizon_reward, train_state1 = self.flatten_data(
            train_state0, train_action, train_reward, train_state1, horizon=horizon)
        # train_state0, train_action, train_horizon_reward, train_state1 = self.shuffle_flat_data(
        #     train_state0, train_action, train_horizon_reward, train_state1)

        # Train the value network
        # for i in range(num_passes):
        loss, mean_value = self.train_value(train_state0, train_action, train_horizon_reward, train_state1,
                         batch_size=batch_size, horizon=horizon, eval=False)
            # logging.info("pass={}, loss={:.3f}, mean_value={:.3f}".format(
            #     i, loss, mean_value))

        logging.info(
            "round={}, reward={:.3f}, horizon_reward={:.3f}, loss={:.5f}, mean_value={:.3f}".format(
                self.round_number, torch.mean(train_reward), torch.mean(train_horizon_reward), loss, mean_value,
                num_episodes, episode_length, horizon, batch_size))


        # # Evaluate the value network
        # test_state0, test_action, test_reward, test_state1 = self.generate_data(
        #     num_episodes=eval_episodes, episode_length=episode_length)
        # test_state0, test_action, test_horizon_reward, test_state1 = self.flatten_data(
        #     test_state0, test_action, test_reward, test_state1, horizon=horizon)
        # loss, mean_value = self.train_value(test_state0, test_action, test_horizon_reward, test_state1,
        #                                     batch_size=batch_size, horizon=horizon, eval=True)
        # logging.info("eval: loss={:.3f}, mean_value={:.3f}".format(loss, mean_value))

        # state0, action, reward, state1 = self.generate_data(eval_episodes, episode_length)
        # cumul_reward = torch.cumsum(reward, dim=1)
        # horizon_reward = (cumul_reward[:, horizon:] - cumul_reward[:, :-horizon]) / horizon
        # logging.info("eval: reward={:.3f}, horizon_reward={:.3f} (episodes={}, length={}, horizon={}, batch={})".format(
        #     torch.mean(reward), torch.mean(horizon_reward), eval_episodes, episode_length, horizon, batch_size))
        # num_rows = eval_episodes * (episode_length - horizon)
        # num_batches = num_rows // batch_size  # Round down, to discard any remaining partial batch
        # state0 = state0[:, :-horizon, :].reshape(num_rows, self.state_width)
        # action = action[:, :-horizon].reshape(num_rows)
        # reward = horizon_reward.reshape(num_rows)
        # state1 = state1[:, :-horizon].reshape(num_rows, self.state_width)
        # total_loss = 0.0
        # total_mean_value = 0.0
        # for j in range(num_batches):
        #     start = j * batch_size
        #     end = (j + 1) * batch_size
        #
        #     state0_batch = state0[start:end, :]
        #     value0_batch = self.value_network(state0_batch)[:, 0]
        #     target_batch = reward[start:end]
        #     loss = torch.mean((value0_batch - target_batch) ** 2)
        #     total_loss += loss.item()
        #     total_mean_value += torch.mean(value0_batch)
        # logging.info("eval: loss={:.3f}, mean_value={:.3f}".format(
        #     total_loss / num_batches, total_mean_value / num_batches))

        self.round_number += 1


# env = gym.make('CartPole-v0').unwrapped
env = MazeBuilderEnv(maze_builder.crateria.rooms[:10], map_x=12, map_y=12, action_radius=1)
observation_dim = np.prod(env.observation_space.shape)
action_dim = env.action_space.n
value_network = MainNetwork([observation_dim, 256, 256, 1])
policy_network = MainNetwork([observation_dim, 1, action_dim])  # TODO: change this
value_optimizer = torch.optim.Adam(value_network.parameters(), lr=0.01, betas=(0.9, 0.999), eps=1e-15)
policy_optimizer = torch.optim.Adam(policy_network.parameters(), lr=0.0001, betas=(0.9, 0.999), eps=1e-15)

value_network.lin_layers[-1].weight.data[:, :] = 0.0
policy_network.lin_layers[-1].weight.data[:, :] = 0.0
# value_network.lin_layers[-1].weights_pos_neg.param.data[:, :] = 0.0
# policy_network.lin_layers[-1].weights_pos_neg.param.data[:, :] = 0.0
value_network.lin_layers[-1].bias.data[:] = 0.0
policy_network.lin_layers[-1].bias.data[:] = 0.0
print(value_network)
print(value_optimizer)
print(policy_network)
print(policy_optimizer)
session = TrainingSession(env,
                          value_network=value_network,
                          policy_network=policy_network,
                          value_optimizer=value_optimizer,
                          policy_optimizer=policy_optimizer,
                          weight_decay=0.0)
num_episodes = 1024
episode_length = 128
horizon = 20
batch_size = 256
print('episodes per cycle={}, episode length={}, horizon={}, batch size={}'.format(
    num_episodes, episode_length, horizon, batch_size))
logging.info("Starting training")
for rnd in range(100):
    session.train_round(num_episodes=num_episodes,
                        episode_length=episode_length,
                        num_passes=1,
                        batch_size=batch_size,
                        horizon=horizon)

# # optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
# batch_size = 2048
# train_freq = 64
# print_freq = 50
# display_freq = 30
# session = TrainingSession(env, model,
#                           optimizer,
#                           replay_capacity=5000, reward_horizon=10,
#                           max_steps=200, value_loss_coef=1.0,
#                           weight_decay=0.0 * optimizer.param_groups[0]['lr'],
#                           entropy_penalty=0.0)
#
# entropy_penalty0 = 0.01
# entropy_penalty1 = 0.01
# lr0 = 0.00005
# lr1 = 0.00005
# transition_time = 200000
# total_policy_loss = 0.0
# total_value_loss = 0.0
# total_entropy = 0.0
#
# print_ctr = 0
# while True:
#     session.play_step()
#     if session.episode_number % display_freq == 0:
#         env.render()
#     if session.replay.size >= batch_size and session.total_steps % train_freq == 0:
#         lr = np.interp(session.total_steps, [0, transition_time], [lr0, lr1])
#         entropy_penalty = np.interp(session.total_steps, [0, transition_time], [entropy_penalty0, entropy_penalty1])
#         session.optimizer.param_groups[0]['lr'] = lr
#         session.entropy_penalty = entropy_penalty
#
#         policy_loss, value_loss, entropy = session.train_step(batch_size)
#         total_policy_loss += policy_loss
#         total_value_loss += value_loss
#         total_entropy += entropy
#         print_ctr += 1
#         if print_ctr == print_freq:
#             print_ctr = 0
#             mean_reward = torch.mean(session.replay.mean_reward[:session.replay.size])
#             # mean_reward = session.replay.mean_reward[session.replay.size - 1]
#             logging.info("{}: episode={}, policy_loss={:.5f}, value_loss={:.5f}, entropy={:.5f}, reward={:.5f}, pen={:.3g}".format(
#                 session.total_steps, session.episode_number,
#                 total_policy_loss / print_freq / batch_size,
#                 total_value_loss / print_freq / batch_size,
#                 total_entropy / print_freq / batch_size,
#                 mean_reward, session.entropy_penalty))
#             total_policy_loss = 0
#             total_value_loss = 0
#             total_entropy = 0
