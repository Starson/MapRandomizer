import torch
import logging
from maze_builder.env import MazeBuilderEnv
import logic.rooms.crateria_isolated
import pickle

logging.basicConfig(format='%(asctime)s %(message)s',
                    level=logging.INFO,
                    handlers=[logging.FileHandler("train.log"),
                              logging.StreamHandler()])

torch.set_printoptions(linewidth=120, threshold=10000)
import io


class CPU_Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == 'torch.storage' and name == '_load_from_bytes':
            return lambda b: torch.load(io.BytesIO(b), map_location='cpu')
        else:
            return super().find_class(module, name)

device = torch.device('cpu')
session = CPU_Unpickler(open('models/crateria-2021-08-03T00:04:17.444944.pkl', 'rb')).load()

num_envs = 32
rooms = logic.rooms.crateria_isolated.rooms
episode_length = len(rooms)
env = MazeBuilderEnv(rooms,
                     map_x=session.env.map_x,
                     map_y=session.env.map_y,
                     num_envs=num_envs,
                     device=device)


episode_length = len(rooms)
session.env = env
num_candidates = 4
temperature = 5

while True:
    room_mask, room_position_x, room_position_y, state_value, action_value, action, reward, action_prob = session.generate_episode(
        episode_length,
        num_candidates=num_candidates,
        temperature=temperature,
        explore_eps=0,
        render=False)
    max_reward, max_reward_ind = torch.max(reward, dim=0)
    logging.info("{}: {}".format(max_reward, reward.tolist()))
    if max_reward.item() >= 33:
        break
    # time.sleep(5)
session.env.render(max_reward_ind.item())
