import random

import torch
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.utils.data import RandomSampler, BatchSampler
from .utils import calculate_accuracy, Cutout
from .trainer import Trainer
from src.utils import EarlyStopping
from torchvision import transforms
import torchvision.transforms.functional as TF
from src.memory import blank_trans


class Classifier(nn.Module):
    def __init__(self, num_inputs1, num_inputs2):
        super().__init__()
        self.network = nn.Bilinear(num_inputs1, num_inputs2, 1)

    def forward(self, x1, x2):
        return self.network(x1, x2)


class PredictionModule(nn.Module):
    def __init__(self, state_dim, num_actions):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(state_dim*num_actions*4, state_dim*4),
            nn.ReLU(),
            nn.Linear(state_dim*4, state_dim*4),
            nn.ReLU(),
            nn.Linear(state_dim*4, state_dim))

    def forward(self, states, actions):
        N = states.size(0)
        output = self.network(
            torch.bmm(states.unsqueeze(2), actions.unsqueeze(1)).view(N, -1))  # outer-product / bilinear integration, then flatten
        return output


class RewardPredictionModule(nn.Module):
    def __init__(self, state_dim, num_actions, reward_dim=3):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(state_dim*num_actions*4, state_dim*4),
            nn.ReLU(),
            nn.Linear(state_dim*4, state_dim*4),
            nn.ReLU(),
            nn.Linear(state_dim*4, reward_dim))

    def forward(self, states, actions):
        N = states.size(0)
        output = self.network(
            torch.bmm(states.unsqueeze(2), actions.unsqueeze(1)).view(N, -1))  # outer-product / bilinear integration, then flatten
        return output


class FramestackActionInfoNCESpatioTemporalTrainer(Trainer):
    def __init__(self, encoder, config, device=torch.device('cpu'), wandb=None, agent=None):
        super().__init__(encoder, wandb, device)
        self.config = config
        self.patience = self.config["patience"]
        self.use_multiple_predictors = config.get("use_multiple_predictors", False)
        print("Using multiple predictors" if self.use_multiple_predictors else "Using shared classifier")
        self.epochs = config['epochs']
        self.batch_size = config['batch_size']
        self.global_loss = config['global_loss']
        self.bilinear_global_loss = config['bilinear_global_loss']
        self.noncontrastive_global_loss = config['noncontrastive_global_loss']
        self.noncontrastive_loss_weight = config['noncontrastive_loss_weight']

        self.device = device
        self.classifier = nn.Linear(self.encoder.hidden_size, 64).to(device)
        self.global_classifier = nn.Linear(self.encoder.hidden_size,
                                           self.encoder.hidden_size).to(device)
        self.params = list(self.encoder.parameters())
        self.params += list(self.classifier.parameters())
        self.params += list(self.global_classifier.parameters())

        self.prediction_module = PredictionModule(self.encoder.hidden_size,
                                                  config["num_actions"])
        self.reward_module = RewardPredictionModule(self.encoder.hidden_size,
                                                    config["num_actions"])

        self.reward_loss_weight = config["reward_loss_weight"]

        self.prediction_module.to(device)
        self.convert_actions = lambda a: F.one_hot(a, num_classes=config["num_actions"])
        self.reward_module.to(device)
        self.params += list(self.prediction_module.parameters())
        self.params += list(self.reward_module.parameters())
        self.hard_neg_factor = config["hard_neg_factor"]

        self.optimizer = torch.optim.Adam(self.params, lr=config['encoder_lr'], eps=1e-5)
        self.early_stopper = EarlyStopping(patience=self.patience, verbose=False, wandb=self.wandb, name="encoder")
        self.epochs_till_now = 0

    def generate_batch(self, transitions, actions=None):
        total_steps = len(transitions)
        print('Total Steps: {}'.format(len(transitions)))
        # Episode sampler
        # Sample `num_samples` episodes then batchify them with `self.batch_size` episodes per batch
        for idx in range(total_steps // self.batch_size):
            indices = np.random.randint(0, total_steps-1, size=self.batch_size)
            x_t, x_tnext, a_t, r_tnext, dones = [], [], [], [], []
            for t in indices:
                # Get one sample from this episode
                while transitions[t].nonterminal is False:
                    t = np.random.randint(0, total_steps-1)

                trans = np.array([None] * 4)
                trans[-1] = transitions[t]
                for i in range(4 - 2, -1, -1):  # e.g. 2 1 0
                    if trans[i + 1].timestep == 0:
                        trans[i] = blank_trans  # If future frame has timestep 0
                    else:
                        trans[i] = transitions[t - 4 + 1 + i]
                states = [t.state for t in trans]

                x_t.append(torch.stack(states, 0))
                x_tnext.append(transitions[t+1].state)
                a_t.append(transitions[t].action)
                r_tnext.append(transitions[t+1].reward + 1)
                dones.append(transitions[t+1].nonterminal)

            yield torch.stack(x_t).to(self.device).float() / 255., \
                  torch.stack(x_tnext).to(self.device).float() / 255., \
                  torch.tensor(a_t, device=self.device).long(), \
                  torch.tensor(r_tnext, device=self.device).long(), \
                  torch.tensor(dones, device=self.device).unsqueeze(-1).float()

    def generate_reward_class_weights(self, transitions):
        counts = [0, 0, 0]  # counts for reward=-1,0,1
        for trans in transitions:
            counts[trans.reward + 1] += 1

        weights = [0., 0., 0.]
        for i in range(3):
            if counts[i] != 0:
                weights[i] = sum(counts) / counts[i]
        return torch.tensor(weights, device=self.device)

    def hard_neg_sampling(self, encoded_obs, actions, initial_obs):
        """
        :param encoded_obs: Output of the encoder.
        :param actions: Embedded (or continuous/one hot) actions
        :return: A tensor of predicted states for negative pairs.
        """
        encoded_obs = encoded_obs.repeat(self.hard_neg_factor, 1)
        actions = actions.repeat(self.hard_neg_factor, 1)
        initial_obs = initial_obs.repeat(self.hard_neg_factor, 1)

        shuffle_indices = torch.randperm(actions.shape[0],
                                         device=actions.device)
        shuffled_actions = actions[shuffle_indices]

        predictions = self.prediction_module(encoded_obs,
                                             shuffled_actions) + initial_obs
        return predictions

    def do_one_epoch(self, episodes):
        mode = "train" if self.encoder.training else "val"
        epoch_loss, steps = 0., 0.
        epoch_local_loss, epoch_rew_loss, epoch_global_loss, rew_acc, = 0., 0., 0., 0.
        pos_rew_tp, pos_rew_tn, pos_rew_fp, pos_rew_fn = 0, 0, 0, 0
        zero_rew_tp, zero_rew_tn, zero_rew_fp, zero_rew_fn = 0, 0, 0, 0
        sd_loss = 0
        sd_cosine_sim = 0
        representation_norm = 0

        data_generator = self.generate_batch(episodes)
        for x_tprev, x_t, actions, rewards, done in data_generator:
            x_tprev = x_tprev.view(x_t.shape[0]*4, *x_tprev.shape[2:])
            f_t_maps, f_t_prev_maps = self.encoder(x_t, fmaps=True),\
                                      self.encoder(x_tprev, fmaps=True)

            f_t_prev_stack = f_t_prev_maps["out"].view(x_t.shape[0], 4, -1)
            f_t_initial = f_t_prev_stack[:, -1]
            f_t_prev = f_t_prev_maps["out"].view(x_t.shape[0], -1)
            f_t = f_t_maps['f5']
            f_t_global = f_t_maps["out"]

            # Loss 1: Global at time t, f5 patches at time t-1
            sy = f_t.size(1)
            sx = f_t.size(2)
            actions = self.convert_actions(actions).float()

            N = f_t_prev.size(0)
            loss1 = 0.

            f_t_pred_delta = self.prediction_module(f_t_prev, actions)
            f_t_pred = f_t_pred_delta + f_t_initial
            if self.hard_neg_factor > 0:
                hard_negs = self.hard_neg_sampling(f_t_prev,
                                                   actions,
                                                   f_t_initial)
                f_t_pred = torch.cat([f_t_pred, hard_negs], 0)

            for y in range(sy):
                for x in range(sx):
                    predictions = self.classifier(f_t_pred)
                    positive = f_t[:, y, x, :]
                    logits = torch.matmul(predictions, positive.t())
                    step_loss = F.cross_entropy(logits.t(),
                                                torch.arange(N).to(self.device))
                    loss1 += step_loss
            loss1 = loss1 / (sx * sy)

            reward_preds = self.reward_module(f_t_prev, actions)
            # reward_loss = F.cross_entropy(reward_preds, rewards, weight=self.class_weights)
            if rewards.max() == 2:
                reward_loss = F.cross_entropy(reward_preds, rewards, weight=self.class_weights)
            else:
                # If the batch contains no pos. reward, normalize manually
                reward_loss = F.cross_entropy(reward_preds, rewards, weight=self.class_weights, reduction='none')
                reward_loss = reward_loss.sum() / (self.class_weights[rewards].sum() + self.class_weights[2])

            # Get TF/TP/TN/FP for rewards to calculate precision, recall later
            reward_preds = reward_preds.argmax(dim=-1)
            rew_acc += (reward_preds == rewards).float().mean()
            pos_rew_tp += ((reward_preds == 2)*(rewards == 2)).float().sum()
            pos_rew_fp += ((reward_preds == 2)*(rewards != 2)).float().sum()
            pos_rew_fn += ((reward_preds != 2)*(rewards == 2)).float().sum()
            pos_rew_tn += ((reward_preds != 2)*(rewards != 2)).float().sum()

            zero_rew_tp += ((reward_preds == 1)*(rewards == 1)).float().sum()
            zero_rew_fp += ((reward_preds == 1)*(rewards != 1)).float().sum()
            zero_rew_fn += ((reward_preds != 1)*(rewards == 1)).float().sum()
            zero_rew_tn += ((reward_preds != 1)*(rewards != 1)).float().sum()

            if self.global_loss:
                diff = f_t_pred.unsqueeze(0) - f_t_global.unsqueeze(1)
                logits = -torch.norm(diff, p=2, dim=-1)
                loss2 = F.cross_entropy(logits, torch.arange(N).to(self.device))
                epoch_global_loss += loss2.detach().item()
            elif self.bilinear_global_loss:
                f_t_pred = self.global_classifier(f_t_pred)
                f_t_pred_delta = f_t_pred - f_t_initial
                logits = torch.matmul(f_t_pred, f_t_global.t())
                loss2 = F.cross_entropy(logits, torch.arange(N).to(self.device))
                epoch_global_loss += loss2.detach().item()
            else:
                loss2 = 0

            # log information about the quality of the predictions/latents
            local_sd_loss = F.mse_loss(f_t_global, f_t_pred[:f_t_global.shape[0]], reduction="mean")
            sd_loss += local_sd_loss
            sd_cosine_sim += F.cosine_similarity(f_t_pred_delta, f_t_global - f_t_initial, dim=-1).mean()
            representation_norm += torch.norm(f_t_global, dim=-1).mean()

            self.optimizer.zero_grad()
            loss = loss1 + loss2 + reward_loss*self.reward_loss_weight
            if self.noncontrastive_global_loss:
                loss = loss + local_sd_loss*self.noncontrastive_loss_weight
            if mode == "train":
                loss.backward()
                self.optimizer.step()

            epoch_loss += loss.detach().item()
            epoch_local_loss += loss1.detach().item()
            epoch_rew_loss += reward_loss.detach().item()

            steps += 1

        pos_recall = pos_rew_tp/(pos_rew_fn + pos_rew_tp)
        pos_precision = pos_rew_tp/(pos_rew_tp + pos_rew_fp)

        zero_recall = zero_rew_tp/(zero_rew_fn + zero_rew_tp)
        zero_precision = zero_rew_tp/(zero_rew_tp + zero_rew_fp)

        self.log_results(epoch_local_loss / steps,
                         epoch_rew_loss / steps,
                         epoch_global_loss / steps,
                         epoch_loss / steps,
                         sd_loss / steps,
                         sd_cosine_sim / steps,
                         rew_acc / steps,
                         pos_recall,
                         pos_precision,
                         zero_recall,
                         zero_precision,
                         prefix=mode)
        if mode == "val":
            self.early_stopper(-epoch_loss / steps, self.encoder)

    def train(self, tr_eps, val_eps=None, epochs=None):
        self.class_weights = self.generate_reward_class_weights(tr_eps)
        if not epochs:
            epochs = self.epochs
        epochs = range(epochs)
        for _ in epochs:
            self.encoder.train(), self.classifier.train()
            self.do_one_epoch(tr_eps)

            if val_eps:
                self.encoder.eval(), self.classifier.eval()
                self.do_one_epoch(val_eps)

                if self.early_stopper.early_stop:
                    break
            self.epochs_till_now += 1
        torch.save(self.encoder.state_dict(),
                   os.path.join(self.wandb.run.dir,
                                self.config['game'] + '.pt'))

    def predict(self, z, a):
        N = z.size(0)
        z_last = z.view(N, 4, -1)[:, -1, :]  # choose the last latent vector from z
        z = z.view(N, -1)
        new_states = self.prediction_module(z, a) + z_last
        if self.bilinear_global_loss:
            new_states = self.global_classifier(new_states)
        return new_states, self.reward_module(z, a).argmax(-1) - 1

    def log_results(self,
                    local_loss,
                    reward_loss,
                    global_loss,
                    epoch_loss,
                    sd_loss,
                    sd_cosine_sim,
                    rew_acc,
                    pos_recall,
                    pos_precision,
                    zero_recall,
                    zero_precision,
                    prefix=""):
        print(
            "{} Epoch: {}, Epoch Loss: {:.3f}, Local Loss: {:.3f}, Reward Loss: {:.3f}, Global Loss: {:.3f}, Dynamics Error: {:.3f}, Prediction Cosine Similarity: {:.3f}, Reward Accuracy: {:.3f}, {}".format(
                prefix.capitalize(),
                self.epochs_till_now,
                epoch_loss,
                local_loss,
                reward_loss,
                global_loss,
                sd_loss,
                sd_cosine_sim,
                rew_acc,
                prefix.capitalize()))
        print(
            "{} Positive Reward Recall: {:.3f}, Positive Reward Precision: {:.3f}, Zero Reward Recall: {:.3f}, Zero Reward Precision: {:.3f}".format(
                prefix.capitalize(),
                pos_recall,
                pos_precision,
                zero_recall,
                zero_precision))
        self.wandb.log({prefix + '_loss': epoch_loss,
                        prefix + '_local_loss': local_loss,
                        "Reward Loss": reward_loss,
                        prefix + '_global_loss': global_loss,
                        "Reward Accuracy": rew_acc,
                        'SD Loss': sd_loss,
                        'SD Cosine Similarity': sd_cosine_sim,
                        "Pos. Reward Recall": pos_recall,
                        "Zero Reward Recall": zero_recall,
                        "Pos. Reward Precision": pos_precision,
                        "Zero Reward Precision": zero_precision,
                        'FM epoch': self.epochs_till_now})