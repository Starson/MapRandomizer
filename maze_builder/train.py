# TODO:
#  - try replacing room mask with aggregated room embeddings (possibly using same convolutional network from local map)
#  - noisy nets: for strategic/coordinated exploration?
#      - instead of randomizing weights/biases, maybe just add noise (with tunable scale) to activations
#        in certain layer(s) (same noise across all time steps of an episode)
#  - distributional DQN: split space of rewards into buckets and predict probabilities
#  - prioritized replay
#  - try using batch norm again but with proper averaging of its non-trainable parameters
#  - try some of the new ideas on Atari benchmarks (variation of dueling network, and variation of noisy nets)
import torch
import logging
from maze_builder.env import MazeBuilderEnv
from maze_builder.types import Room
import logic.rooms.crateria
from datetime import datetime
from typing import List, Optional
import pickle
from model_average import SimpleAverage

logging.basicConfig(format='%(asctime)s %(message)s',
                    level=logging.INFO,
                    handlers=[logging.FileHandler("train.log"),
                              logging.StreamHandler()])
torch.autograd.set_detect_anomaly(True)

start_time = datetime.now()
pickle_name = 'models/crateria-{}.pkl'.format(start_time.isoformat())
logging.info("Checkpoint path: {}".format(pickle_name))


class GlobalAvgPool2d(torch.nn.Module):
    def forward(self, X):
        return torch.mean(X, dim=[2, 3])


class GlobalMaxPool2d(torch.nn.Module):
    def forward(self, X):
        return torch.max(X.view(X.shape[0], X.shape[1], X.shape[2] * X.shape[3]), dim=2)[0]


# TODO: look at using torch.multinomial instead of implementing this from scratch?
def _rand_choice(p):
    cumul_p = torch.cumsum(p, dim=1)
    rnd = torch.rand([p.shape[0], 1], device=p.device)
    choice = torch.clamp(torch.searchsorted(cumul_p, rnd), max=p.shape[1] - 1).view(-1)
    return choice


class Network(torch.nn.Module):
    def __init__(self, room_tensor, map_x, map_y, global_map_channels, global_map_kernel_size, global_fc_widths,
                 room_embedding_width, local_fc_widths):
        super().__init__()
        self.room_tensor = room_tensor
        self.map_x = map_x
        self.map_y = map_y
        num_rooms = room_tensor.shape[0]
        room_width = room_tensor.shape[1]
        # batch_norm_momentum = 1.0

        global_map_layers = []
        global_map_channels = [5] + global_map_channels
        for i in range(len(global_map_channels) - 1):
            global_map_layers.append(torch.nn.Conv2d(global_map_channels[i], global_map_channels[i + 1],
                                                     kernel_size=(global_map_kernel_size[i], global_map_kernel_size[i]),
                                                     padding=global_map_kernel_size[i] // 2))
            global_map_layers.append(torch.nn.ReLU())
            # global_map_layers.append(torch.nn.BatchNorm2d(global_map_channels[i + 1], momentum=batch_norm_momentum))
            # global_map_layers.append(torch.nn.MaxPool2d(3, stride=2, padding=1))
            # global_map_layers.append(torch.nn.MaxPool2d(2, stride=2))
            # width = (width - map_kernel_size[i]) // 2
            # height = (height - map_kernel_size[i]) // 2
        # global_map_layers.append(GlobalAvgPool2d())
        # global_map_layers.append(torch.nn.Flatten())
        self.global_map_sequential = torch.nn.Sequential(*global_map_layers)
        self.global_pool = GlobalMaxPool2d()

        global_fc_layers = []
        # global_fc_widths = [(width * height * map_channels[-1]) + 1 + room_tensor.shape[0]] + global_fc_widths
        global_fc_widths = [global_map_channels[-1] + 1 + room_tensor.shape[0]] + global_fc_widths
        for i in range(len(global_fc_widths) - 1):
            global_fc_layers.append(torch.nn.Linear(global_fc_widths[i], global_fc_widths[i + 1]))
            # global_fc_layers.append(torch.nn.BatchNorm1d(global_fc_widths[i + 1], momentum=batch_norm_momentum))
            global_fc_layers.append(torch.nn.ReLU())
        # global_fc_layers.append(torch.nn.Linear(fc_widths[-1], 1))
        self.global_fc_sequential = torch.nn.Sequential(*global_fc_layers)
        self.state_value_lin = torch.nn.Linear(global_fc_widths[-1], 1)

        local_fc_layers = []
        local_fc_widths = [global_map_channels[-1] + global_fc_widths[-1] + room_embedding_width] + local_fc_widths
        for i in range(len(local_fc_widths) - 1):
            local_fc_layers.append(torch.nn.Linear(local_fc_widths[i], local_fc_widths[i + 1]))
            local_fc_layers.append(torch.nn.ReLU())
            # local_fc_layers.append(torch.nn.BatchNorm1d(local_fc_widths[i + 1], momentum=batch_norm_momentum))
        self.local_fc_sequential = torch.nn.Sequential(*local_fc_layers)
        self.action_value_lin = torch.nn.Linear(local_fc_widths[-1], 1)

        self.room_embedding = torch.nn.Parameter(torch.randn([num_rooms, room_embedding_width]))

    def forward(self, map, room_mask, candidate_placements, steps_remaining):
        num_envs = map.shape[0]
        num_channels = map.shape[1]
        map_x = map.shape[2]
        map_y = map.shape[3]
        num_candidates = candidate_placements.shape[1]

        # Convolutional layers on whole map data
        map = map.to(torch.float32)
        X = map
        # x_channel = torch.arange(self.map_x, device=map.device).view(1, 1, -1, 1).repeat(map.shape[0], 1, 1, self.map_y)
        # y_channel = torch.arange(self.map_y, device=map.device).view(1, 1, 1, -1).repeat(map.shape[0], 1, self.map_x, 1)
        # X = torch.cat([X, x_channel, y_channel], dim=1)
        for layer in self.global_map_sequential:
            # print(X.shape, layer)
            X = layer(X)
        # print(X.shape)
        global_map = X
        pooled_map = self.global_pool(global_map)

        # Fully-connected layers on whole map data (starting with output of convolutional layers)
        X = torch.cat([pooled_map, steps_remaining.view(-1, 1), room_mask], dim=1)
        for layer in self.global_fc_sequential:
            X = layer(X)
        global_embedding = X

        state_value = self.state_value_lin(global_embedding)[:, 0]

        # For each candidate placement, create local map
        room_width = self.room_tensor.shape[2]
        room_choice = candidate_placements[:, :, 0]
        index_x = candidate_placements[:, :, 1] + room_width // 2
        index_y = candidate_placements[:, :, 2] + room_width // 2
        room_embedding = self.room_embedding[room_choice, :]
        local_map = global_map[torch.arange(num_envs, device=device).unsqueeze(1), :, index_x, index_y]
        # print(local_map.shape, global_embedding.unsqueeze(1).repeat(1, num_candidates, 1).shape, room_embedding.shape)
        X = torch.cat([local_map, global_embedding.unsqueeze(1).repeat(1, num_candidates, 1), room_embedding], dim=2)

        # Fully-connected layers per-candidate (starting with local map + global embedding from whole map)
        for layer in self.local_fc_sequential:
            X = layer(X)
        action_value_flat = self.action_value_lin(X)
        action_value = action_value_flat.view(num_envs, num_candidates)

        return state_value, action_value

    def all_param_data(self):
        params = [param.data for param in self.parameters()]
        for module in self.modules():
            if isinstance(module, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d)):
                params.append(module.running_mean)
                params.append(module.running_var)
        return params

# torch.manual_seed(2)
# rb = ReplayBuffer(0.5)
# A = torch.randn([3, 2])
# loss_A = torch.rand([3])
# rb.insert(loss_A, A)
# B = torch.randn([5, 2])
# loss_B = torch.rand([5])
# rb.insert(loss_B, B)
# sm = 0.0
# for n in [2, 2, 2, 2, 2, 1]:
#     weight, sample_loss, sample = rb.take_sample(n)
#     sm += torch.sum(weight.unsqueeze(1) * sample, dim=0)
#     print(weight)
# print(sm, torch.sum(A, dim=0) + torch.sum(B, dim=0))
#
# self = rb
# n = 2

class TrainingSession():
    def __init__(self, env: MazeBuilderEnv,
                 network: Network,
                 optimizer: torch.optim.Optimizer,
                 ):
        self.env = env
        self.network = network
        self.optimizer = optimizer
        self.average_parameters = SimpleAverage(network.all_param_data())
        self.num_rounds = 0

    def generate_round(self, episode_length: int, num_candidates: int, temperature: float, render=False):
        device = self.env.map.device
        map, room_mask = self.env.reset()
        map_list = [map]
        room_mask_list = [room_mask]
        state_value_list = []
        action_value_list = []
        action_list = []
        self.network.eval()
        with self.average_parameters.average_parameters(self.network.all_param_data()):
            total_action_prob = 0.0
            for j in range(episode_length):
                if render:
                    self.env.render()
                candidate_placements = env.get_placement_candidates(num_candidates)
                steps_remaining = torch.full([self.env.num_envs], episode_length - j,
                                             dtype=torch.float32, device=device)
                with torch.no_grad():
                    state_value, action_value = self.network(map, room_mask, candidate_placements, steps_remaining)
                action_probs = torch.softmax(action_value * temperature, dim=1)
                action_index = _rand_choice(action_probs)
                selected_action_prob = action_probs[torch.arange(self.env.num_envs, device=device), action_index]
                action = candidate_placements[torch.arange(self.env.num_envs, device=device), action_index]
                selected_action_value = action_value[torch.arange(self.env.num_envs, device=device), action_index]
                map, room_mask = self.env.step(action[:, 0], action[:, 1], action[:, 2])
                map_list.append(map)
                room_mask_list.append(room_mask)
                action_list.append(action)
                state_value_list.append(state_value)
                action_value_list.append(selected_action_value)
                total_action_prob += torch.mean(selected_action_prob).item()
        map_tensor = torch.stack(map_list, dim=0)
        room_mask_tensor = torch.stack(room_mask_list, dim=0)
        state_value_tensor = torch.stack(state_value_list, dim=0)
        action_value_tensor = torch.stack(action_value_list, dim=0)
        action_tensor = torch.stack(action_list, dim=0)
        reward_tensor = self.env.reward()
        action_prob = total_action_prob / episode_length
        return map_tensor, room_mask_tensor, state_value_tensor, action_value_tensor, action_tensor, reward_tensor, action_prob

    def train_round(self,
                    episode_length: int,
                    batch_size: int,
                    num_candidates: int,
                    temperature: float,
                    action_loss_weight: float = 0.5,
                    td_lambda: float = 0.0,
                    render: bool = False,
                    ):
        map, room_mask, state_value, action_value, action, reward, action_prob = self.generate_round(
            episode_length=episode_length,
            num_candidates=num_candidates,
            temperature=temperature,
            render=render)

        map0 = map[:-1, :, :, :, :]
        room_mask0 = room_mask[:-1, :, :]
        steps_remaining = (episode_length - torch.arange(episode_length, device=map.device)).view(-1, 1).repeat(1,
                                                                                                                env.num_envs)

        mean_reward = torch.mean(reward.to(torch.float32))
        max_reward = torch.max(reward).item()
        cnt_max_reward = torch.sum(reward == max_reward)

        # Compute Monte-Carlo errors
        total_mc_state_err = 0.0
        total_mc_action_err = 0.0
        for i in reversed(range(episode_length)):
            state_value1 = state_value[i, :]
            action_value1 = action_value[i, :]
            total_mc_state_err += torch.mean((state_value1 - reward) ** 2).item()
            total_mc_action_err += torch.mean((action_value1 - reward) ** 2).item()

        # Compute the TD targets
        target_list = []
        target_batch = reward
        target_list.append(reward)
        for i in reversed(range(1, episode_length)):
            state_value1 = state_value[i, :]
            target_batch = td_lambda * target_batch + (1 - td_lambda) * state_value1
            target_list.append(target_batch)
        target = torch.stack(list(reversed(target_list)), dim=0)

        # Flatten the data
        n = episode_length * self.env.num_envs
        map0 = map0.view(n, self.env.map_channels, self.env.padded_map_x, self.env.padded_map_y)
        room_mask0 = room_mask0.view(n, len(self.env.rooms) + 1)
        action = action.view(n, 3)
        steps_remaining = steps_remaining.view(n)
        target = target.view(n)

        # Shuffle the data
        perm = torch.randperm(n)
        map0 = map0[perm, :, :, :]
        room_mask0 = room_mask0[perm, :]
        action = action[perm]
        steps_remaining = steps_remaining[perm]
        target = target[perm]

        num_batches = n // batch_size

        total_state_loss = 0.0
        total_action_loss = 0.0
        self.network.train()
        self.average_parameters.reset()
        for i in range(num_batches):
            start = i * batch_size
            end = (i + 1) * batch_size
            map0_batch = map0[start:end, :, :, :]
            room_mask0_batch = room_mask0[start:end, :]
            steps_remaining_batch = steps_remaining[start:end]
            action_batch = action[start:end, :]
            target_batch = target[start:end]

            state_value0, action_value0 = self.network(map0_batch, room_mask0_batch, action_batch.unsqueeze(1),
                                                       steps_remaining_batch)
            action_value0 = action_value0[:, 0]
            state_loss = torch.mean((state_value0 - target_batch) ** 2)
            action_loss = torch.mean((action_value0 - target_batch) ** 2)
            # print(state_loss, action_loss)
            loss = (1 - action_loss_weight) * state_loss + action_loss_weight * action_loss
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.network.parameters(), 1e-5)
            self.optimizer.step()
            self.average_parameters.update(self.network.all_param_data())
            # # self.value_network.decay(weight_decay * self.value_optimizer.param_groups[0]['lr'])
            total_state_loss += state_loss.item()
            total_action_loss += action_loss.item()

        self.num_rounds += 1

        # total_loss = 0
        # num_batches = 1
        # total_mc_state_err = 0
        return mean_reward, max_reward, cnt_max_reward, total_state_loss / num_batches, \
               total_action_loss / num_batches, total_mc_state_err / episode_length, total_mc_action_err / episode_length, \
                action_prob


import logic.rooms.crateria
import logic.rooms.crateria_isolated
import logic.rooms.wrecked_ship
import logic.rooms.norfair_lower
import logic.rooms.norfair_upper
import logic.rooms.norfair_upper_isolated
import logic.rooms.all_rooms
import logic.rooms.brinstar_pink
import logic.rooms.brinstar_green
import logic.rooms.brinstar_red
import logic.rooms.brinstar_blue
import logic.rooms.maridia_lower
import logic.rooms.maridia_upper

# device = torch.device('cpu')
device = torch.device('cuda:0')

num_envs = 512
# num_envs = 32
# rooms = logic.rooms.crateria_isolated.rooms
# rooms = logic.rooms.crateria.rooms
# rooms = logic.rooms.crateria.rooms + logic.rooms.wrecked_ship.rooms
# rooms = logic.rooms.wrecked_ship.rooms
# rooms = logic.rooms.norfair_lower.rooms + logic.rooms.norfair_upper.rooms
rooms = logic.rooms.norfair_upper_isolated.rooms
# rooms = logic.rooms.norfair_upper.rooms
# rooms = logic.rooms.norfair_lower.rooms
# rooms = logic.rooms.brinstar_warehouse.rooms
# rooms = logic.rooms.brinstar_pink.rooms
# rooms = logic.rooms.brinstar_red.rooms
# rooms = logic.rooms.brinstar_blue.rooms
# rooms = logic.rooms.brinstar_green.rooms
# rooms = logic.rooms.maridia_lower.rooms
# rooms = logic.rooms.maridia_upper.rooms
# rooms = logic.rooms.all_rooms.rooms
episode_length = 60
display_freq = 1
map_x = 40
map_y = 30
# map_x = 10
# map_y = 10
env = MazeBuilderEnv(rooms,
                     map_x=map_x,
                     map_y=map_y,
                     max_room_width=15,
                     num_envs=num_envs,
                     device=device)
print("Rooms: {}, Left doors={}, Right doors={}, Up doors={}, Down doors={}".format(
    env.room_tensor.shape[0],
    torch.sum(env.room_tensor[:, 0, 1:, :] & env.room_tensor[:, 1, :-1, :]),
    torch.sum(env.room_tensor[:, 0, :, :] & env.room_tensor[:, 1, :, :]),
    torch.sum(env.room_tensor[:, 0, :, 1:] & env.room_tensor[:, 2, :, :-1]),
    torch.sum(env.room_tensor[:, 0, :, :] & env.room_tensor[:, 2, :, :])))

network = Network(env.room_tensor,
                  map_x=env.padded_map_x,
                  map_y=env.padded_map_y,
                  global_map_channels=[64, 32],
                  global_map_kernel_size=[11, 7],
                  global_fc_widths=[256, 32],
                  room_embedding_width=64,
                  local_fc_widths=[64, 64],
                  ).to(device)
network.state_value_lin.weight.data[:, :] = 0.0
network.state_value_lin.bias.data[:] = 0.0
network.action_value_lin.weight.data[:, :] = 0.0
network.action_value_lin.bias.data[:] = 0.0
optimizer = torch.optim.Adam(network.parameters(), lr=0.0001, betas=(0.5, 0.5), eps=1e-15)

print(network)
print(optimizer)
logging.info("Starting training")

session = TrainingSession(env,
                          network=network,
                          optimizer=optimizer)

torch.set_printoptions(linewidth=120, threshold=10000)
# map_tensor, room_mask_tensor, action_tensor, reward_tensor = session.generate_round(episode_length, num_candidates,
#                                                                                     temperature)

#
# #
# # # session = pickle.load(open('models/crateria-2021-06-29T13:35:06.399214.pkl', 'rb'))
# #
# import io
# class CPU_Unpickler(pickle.Unpickler):
#     def find_class(self, module, name):
#         if module == 'torch.storage' and name =='_load_from_bytes':
#             return lambda b: torch.load(io.BytesIO(b), map_location='cpu')
#         else:
#             return super().find_class(module, name)
# session = CPU_Unpickler(open('models/crateria-2021-07-16T23:23:08.327425.pkl', 'rb')).load()
# # session.policy_optimizer.param_groups[0]['lr'] = 5e-6
# # # session.value_optimizer.param_groups[0]['betas'] = (0.8, 0.999)
batch_size = 2 ** 10
# batch_size = 2 ** 13  # 2 ** 12
td_lambda0 = 1.0
td_lambda1 = 1.0
num_candidates = 16
temperature0 = 0.0
temperature1 = 100.0
# lr0 = 0.0002
# lr1 = 0.0002
annealing_time = 100
action_loss_weight = 0.9
session.env = env
# session.optimizer.param_groups[0]['lr'] = 0.0002
# session.value_optimizer.param_groups[0]['betas'] = (0.5, 0.5)

logging.info(
    "num_envs={}, batch_size={}, num_candidates={}, action_loss_weight={}".format(
        session.env.num_envs, batch_size, num_candidates, action_loss_weight))
for i in range(100000):
    frac = min(1, session.num_rounds / annealing_time)
    temperature = (1 - frac) * temperature0 + frac * temperature1
    td_lambda = (1 - frac) * td_lambda0 + frac * td_lambda1
    # lr = (1 - frac) * lr0 + frac * lr1
    # optimizer.param_groups[0]['lr'] = lr
    mean_reward, max_reward, cnt_max_reward, state_loss, action_loss, mc_state_err, mc_action_err, prob = session.train_round(
        episode_length=episode_length,
        batch_size=batch_size,
        num_candidates=num_candidates,
        temperature=temperature,
        action_loss_weight=action_loss_weight,
        td_lambda=td_lambda,
        # mc_weight=0.1,
        # render=True)
        render=False)
    # render=i % display_freq == 0)
    logging.info(
        "{}: reward={:.2f} (max={:d}, cnt={:d}), state={:.4f}, action={:.4f}, mc_state={:.4f}, mc_action={:.4f}, p={:.4f}".format(
            session.num_rounds, mean_reward, max_reward, cnt_max_reward, state_loss, action_loss, mc_state_err,
            mc_action_err, prob))
    if i % 10 == 0:
        pickle.dump(session, open(pickle_name, 'wb'))
print(network.local_fc_noise_scale)

datas = [param.data for param in network.parameters()]
ids = [id(p) for p in datas]


while True:
    map, room_mask, state_value, action_value, action, reward = session.generate_round(episode_length,
                                                                                       num_candidates=num_candidates,
                                                                                       temperature=100.0, render=False)
    max_reward, max_reward_ind = torch.max(reward, dim=0)
    logging.info("{}: {}".format(max_reward, reward.tolist()))
    if max_reward.item() >= 60:
        break
session.env.render(max_reward_ind.item())
