import matplotlib
matplotlib.use('Agg')  # noqa: E402
import matplotlib.pyplot as plt

import argparse
from os.path import exists
from urllib.request import urlopen
import time

import numpy as np
import torch
import torch.nn as nn

from collections import OrderedDict

import funsor
import funsor.distributions as dist
import funsor.ops as ops
from funsor.pyro.convert import matrix_and_mvn_to_funsor, mvn_to_funsor, funsor_to_cat_and_mvn

from logger import get_logger


def download_data(data_dir):
    if not exists(data_dir + "eeg.dat"):
        url = "http://archive.ics.uci.edu/ml/machine-learning-databases/00264/EEG%20Eye%20State.arff"
        with open(data_dir + "eeg.dat", "wb") as f:
            f.write(urlopen(url).read())


class SLDS(nn.Module):
    def __init__(self, num_components, hidden_dim, obs_dim,
                 fine_transition_noise=False, fine_observation_matrix=False,
                 fine_observation_noise=False, fine_transition_matrix=True,
                 moment_matching_lag=2):

        self.num_components = num_components
        self.hidden_dim = hidden_dim
        self.obs_dim = obs_dim
        self.moment_matching_lag = moment_matching_lag
        self.fine_transition_noise = fine_transition_noise
        self.fine_observation_matrix = fine_observation_matrix
        self.fine_observation_noise = fine_observation_noise
        self.fine_transition_matrix = fine_transition_matrix

        assert moment_matching_lag > 0
        assert fine_transition_noise or fine_observation_matrix or fine_observation_noise or fine_transition_matrix, \
            "The continuous dynamics need to be coupled to the discrete dynamics in at least one way [use at " + \
            "least one of --ftn --ftm --fon --fom]"

        super(SLDS, self).__init__()

        self.transition_logits = nn.Parameter(0.1 * torch.randn(num_components, num_components))
        if fine_transition_matrix:
            transition_matrix = torch.eye(hidden_dim) + 0.05 * torch.randn(num_components, hidden_dim, hidden_dim)
        else:
            transition_matrix = torch.eye(hidden_dim) + 0.05 * torch.randn(hidden_dim, hidden_dim)
        self.transition_matrix = nn.Parameter(transition_matrix)
        if fine_transition_noise:
            self.log_transition_noise = nn.Parameter(0.1 * torch.randn(num_components, hidden_dim))
        else:
            self.log_transition_noise = nn.Parameter(0.1 * torch.randn(hidden_dim))
        if fine_observation_matrix:
            self.observation_matrix = nn.Parameter(0.3 * torch.randn(num_components, hidden_dim, obs_dim))
        else:
            self.observation_matrix = nn.Parameter(0.3 * torch.randn(hidden_dim, obs_dim))
        if fine_observation_noise:
            self.log_obs_noise = nn.Parameter(0.1 * torch.randn(num_components, obs_dim))
        else:
            self.log_obs_noise = nn.Parameter(0.1 * torch.randn(obs_dim))

        x_init_mvn = torch.distributions.MultivariateNormal(torch.zeros(self.hidden_dim), torch.eye(self.hidden_dim))
        self.x_init_mvn = mvn_to_funsor(x_init_mvn, real_inputs=OrderedDict([('x_0', funsor.reals(self.hidden_dim))]))

    def get_tensors_and_dists(self):
        trans_logits = self.transition_logits - self.transition_logits.logsumexp(dim=-1, keepdim=True)
        trans_probs = funsor.Tensor(trans_logits, OrderedDict([("s", funsor.bint(self.num_components))]))

        trans_mvn = torch.distributions.MultivariateNormal(torch.zeros(self.hidden_dim),
                                                           self.log_transition_noise.exp().diag_embed())
        obs_mvn = torch.distributions.MultivariateNormal(torch.zeros(self.obs_dim),
                                                         self.log_obs_noise.exp().diag_embed())

        event_dims = ("s",) if self.fine_transition_matrix or self.fine_transition_noise else ()
        x_trans_dist = matrix_and_mvn_to_funsor(self.transition_matrix, trans_mvn, event_dims, "x", "y")
        event_dims = ("s",) if self.fine_observation_matrix or self.fine_observation_noise else ()
        y_dist = matrix_and_mvn_to_funsor(self.observation_matrix, obs_mvn, event_dims, "x", "y")

        return trans_logits, trans_probs, trans_mvn, obs_mvn, x_trans_dist, y_dist

    @funsor.interpreter.interpretation(funsor.terms.moment_matching)
    def log_prob(self, data):
        trans_logits, trans_probs, trans_mvn, obs_mvn, x_trans_dist, y_dist = self.get_tensors_and_dists()

        log_prob = funsor.Number(0.)

        s_vars = {-1: funsor.Tensor(torch.tensor(0), dtype=self.num_components)}
        x_vars = {}

        for t, y in enumerate(data):
            s_vars[t] = funsor.Variable('s_{}'.format(t), funsor.bint(self.num_components))
            x_vars[t] = funsor.Variable('x_{}'.format(t), funsor.reals(self.hidden_dim))

            log_prob += dist.Categorical(trans_probs(s=s_vars[t - 1]), value=s_vars[t])

            if t == 0:
                log_prob += self.x_init_mvn(value=x_vars[t])
            else:
                log_prob += x_trans_dist(s=s_vars[t], x=x_vars[t - 1], y=x_vars[t])

            if t > self.moment_matching_lag - 1:
                log_prob = log_prob.reduce(ops.logaddexp, frozenset([s_vars[t - self.moment_matching_lag].name,
                                                                     x_vars[t - self.moment_matching_lag].name]))

            log_prob += y_dist(s=s_vars[t], x=x_vars[t], y=y)

        T = data.shape[0]
        for t in range(self.moment_matching_lag):
            log_prob = log_prob.reduce(ops.logaddexp, frozenset([s_vars[T - self.moment_matching_lag + t].name,
                                                                 x_vars[T - self.moment_matching_lag + t].name]))

        assert not log_prob.inputs, 'unexpected free variables remain'

        return log_prob.data

    @torch.no_grad()
    @funsor.interpreter.interpretation(funsor.terms.moment_matching)
    def filter_and_predict(self, data, smoothing=False):
        trans_logits, trans_probs, trans_mvn, obs_mvn, x_trans_dist, y_dist = self.get_tensors_and_dists()

        log_prob = funsor.Number(0.)

        s_vars = {-1: funsor.Tensor(torch.tensor(0), dtype=self.num_components)}
        x_vars = {-1: None}

        predictive_dists, filtering_dists = [], []
        test_LLs = []

        for t, y in enumerate(data):
            s_vars[t] = funsor.Variable('s_{}'.format(t), funsor.bint(self.num_components))
            x_vars[t] = funsor.Variable('x_{}'.format(t), funsor.reals(self.hidden_dim))

            log_prob += dist.Categorical(trans_probs(s=s_vars[t - 1]), value=s_vars[t])

            if t == 0:
                log_prob += self.x_init_mvn(value=x_vars[t])
            else:
                log_prob += x_trans_dist(s=s_vars[t], x=x_vars[t - 1], y=x_vars[t])

            if t > 0:
                log_prob = log_prob.reduce(ops.logaddexp, frozenset([s_vars[t - 1].name, x_vars[t - 1].name]))

            # do 1-step prediction and compute test LL
            if t > 0:
                predictive_dists.append((log_prob, funsor_to_cat_and_mvn(log_prob, 0, ("s_{}".format(t),))))
                _log_prob = log_prob - log_prob.reduce(ops.logaddexp)
                test_LLs.append((y_dist(s=s_vars[t], x=x_vars[t], y=y) + _log_prob).reduce(ops.logaddexp).data.item())

            log_prob += y_dist(s=s_vars[t], x=x_vars[t], y=y)

            # save filtering dists for forward-backward smoothing
            if smoothing:
                filtering_dists.append(log_prob)

        # do the forward-backward recursion using previously computed ingredients
        if smoothing:
            smoothing_dists = [filtering_dists[-1]]
            log_prob = funsor.Number(0.)
            T = data.size(0)

            s_vars = {t: funsor.Variable('s_{}'.format(t), funsor.bint(self.num_components)) for t in range(T)}
            x_vars = {t: funsor.Variable('x_{}'.format(t), funsor.reals(self.hidden_dim)) for t in range(T)}

            for t in reversed(range(T - 1)):
                smoothing_dist = smoothing_dists[-1]
                pred_dist = predictive_dists[t][0]
                integral = smoothing_dist - pred_dist
                integral += dist.Categorical(trans_probs(s=s_vars[t]), value=s_vars[t + 1])
                integral += x_trans_dist(s=s_vars[t], x=x_vars[t], y=x_vars[t + 1])
                integral = integral.reduce(ops.logaddexp, frozenset([s_vars[t + 1].name, x_vars[t + 1].name]))
                smoothing_dists.append(filtering_dists[t] + integral)

        # compute predictive test MSE
        means = torch.stack([d[1][1].mean for d in predictive_dists])  # T-1 2 xdim
        means = torch.matmul(means.unsqueeze(-2), self.observation_matrix).squeeze(-2)  # T-1 2 ydim

        probs = torch.stack([d[1][0].logits for d in predictive_dists]).exp()
        probs = probs / probs.sum(-1, keepdim=True)  # T-1 2

        predictive_means = (probs.unsqueeze(-1) * means).sum(-2)  # T-1 ydim
        pred_mean_init = torch.zeros(1, predictive_means.size(-1))
        predictive_means = torch.cat([pred_mean_init, predictive_means], dim=-2)  # T ydim

        predictive_mse = (predictive_means - data).pow(2.0).mean(-1)

        # print("pred mean\n", means[:, 0].data.numpy())

        if smoothing:
            # compute smoothed mean function
            smoothing_dists = [funsor_to_cat_and_mvn(d, 0, ("s_{}".format(t),))
                               for t, d in enumerate(reversed(smoothing_dists))]
            means = torch.stack([d[1].mean for d in smoothing_dists])  # T 2 xdim
            means = torch.matmul(means.unsqueeze(-2), self.observation_matrix).squeeze(-2)  # T 2 ydim

            probs = torch.stack([d[0].logits for d in smoothing_dists]).exp()
            probs = probs / probs.sum(-1, keepdim=True)  # T 2

            smoothing_means = (probs.unsqueeze(-1) * means).sum(-2)  # T ydim

            # smoothing_mse = (means[1:, :] - data[1:, :]).pow(2.0).mean(-1)
            # print("mse: %.4f" % mse.mean().item(), "sm_mse: %.4f" % smoothing_mse.mean().item())

            return predictive_mse, torch.tensor(np.array(test_LLs)), predictive_means, smoothing_means
        else:
            return predictive_mse, torch.tensor(np.array(test_LLs))


def main(**args):
    log_file = 'eeg.hd_{}.lr_{:.3f}.mml_{}.b1_{:.2f}.ftm_{}.ftn_{}.fom_{}.fon_{}.seed_{}.log'
    log_file = log_file.format(args['hidden_dim'], args['learning_rate'],
                               args['moment_matching_lag'], args['beta1'],
                               args['ftm'], args['ftn'], args['fom'], args['fon'], args['seed'])
    log = get_logger(args['log_dir'], log_file, use_local_logger=False)

    log(args)
    torch.manual_seed(args['seed'])

    download_data(args['data_dir'])
    data = np.loadtxt(args['data_dir'] + 'eeg.dat', delimiter=',', skiprows=19)
    log("[raw data shape] {}".format(data.shape))
    data = data[::10, :]
    log("[data shape after thinning] {}".format(data.shape))
    data = data[0:500, :]
    log("[data shape after subselection] {}".format(data.shape))

    labels = data[:, -1].tolist()
    labels = [int(l) for l in labels]

    data = torch.tensor(data[:, :-1]).float()
    data_mean = data.mean(0)
    data -= data_mean
    data_std = data.std(0)
    data /= data_std

    hidden_dim = args['hidden_dim']
    T, obs_dim = data.shape

    N_test = 100
    N_train = T - N_test

    log("Length of time series T: {}   Observation dimension: {}".format(T, obs_dim))
    log("N_train: {}  N_test: {}".format(N_train, N_test))

    slds = SLDS(num_components=args['num_components'], hidden_dim=hidden_dim, obs_dim=obs_dim,
                fine_observation_noise=args['fon'], fine_transition_noise=args['ftn'],
                fine_observation_matrix=args['fom'], fine_transition_matrix=args['ftm'],
                moment_matching_lag=args['moment_matching_lag'])

    if args['load']:
        if exists('slds.torch'):
            log('Loading model from slds.torch...')
            slds.load_state_dict(torch.load('slds.torch'))

    adam = torch.optim.Adam(slds.parameters(), lr=args['learning_rate'], betas=(args['beta1'], 0.999), amsgrad=True)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(adam, milestones=[50, 150], gamma=0.2)
    ts = [time.time()]

    report_frequency = 5

    for step in range(args['num_steps']):
        nll = -slds.log_prob(data[0:N_train, :]) / N_train
        nll.backward()

        adam.step()
        scheduler.step()
        adam.zero_grad()

        ts.append(time.time())
        step_dt = ts[-1] - ts[-2]

        if step % report_frequency == 0 or step == args['num_steps'] - 1:
            predicted_mse, LLs = slds.filter_and_predict(data[0:N_train + N_test, :])
            predicted_mse = predicted_mse[-N_test:].mean().item()
            test_ll = LLs[-N_test:].mean().item()

            log("[step %03d]  training nll: %.4f   test mse: %.4f  test LL: %.4f \t\t (step_dt: %.2f)" % (step,
                nll.item(), predicted_mse, test_ll, step_dt))

        if step % 20 == 0 and args['verbose']:
            log("[transition logits] mean: %.2f std: %.2f" % (slds.transition_logits.mean().item(),
                                                              slds.transition_logits.std().item()))
            log("[transition logits]\n", slds.transition_logits.data.numpy())
            log("[transition matrix.abs] mean: %.2f std: %.2f" % (slds.transition_matrix.abs().mean().item(),
                                                                  slds.transition_matrix.abs().std().item()))
            # log("[transition matrix]\n", slds.transition_matrix.data.numpy())
            log("[log_transition_noise] mean: %.2f std: %.2f" % (slds.log_transition_noise.mean().item(),
                                                                 slds.log_transition_noise.std().item()))
            log("[observation matrix.abs] mean: %.2f std: %.2f" % (slds.observation_matrix.abs().mean().item(),
                                                                   slds.observation_matrix.abs().std().item()))
            log("[log_obs_noise] mean: %.2f std: %.2f  min: %.2f  max: %.2f" % (slds.log_obs_noise.mean().item(),
                                                                                slds.log_obs_noise.std().item(),
                                                                                slds.log_obs_noise.min().item(),
                                                                                slds.log_obs_noise.max().item()))

    if args['save']:
        torch.save(slds.state_dict(), 'slds.torch')

    if args['plot']:
        predicted_mse, LLs, pred_means, smooth_means = slds.filter_and_predict(data[0:N_train + N_test, :],
                                                                               smoothing=True)

        f, axes = plt.subplots(15, 1, figsize=(12, 18), sharex=True)
        T = data.size(0)

        for which, ax in enumerate(axes[:-1]):
            ax.plot(np.arange(T), data[:, which], 'ko', markersize=1)
            ax.plot(np.arange(T), pred_means[:, which], ls='dotted', color='b')
            ax.plot(np.arange(T), smooth_means[:, which], ls='solid', color='r')

        axes[-1].plot(np.arange(T), labels)

        plt.savefig('eeg.pdf')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Switching linear dynamical system")
    parser.add_argument("-n", "--num-steps", default=30, type=int)
    parser.add_argument("-s", "--seed", default=0, type=int)
    parser.add_argument("-hd", "--hidden-dim", default=5, type=int)
    parser.add_argument("-k", "--num-components", default=2, type=int)
    parser.add_argument("-lr", "--learning-rate", default=0.15, type=float)
    parser.add_argument("-b1", "--beta1", default=0.50, type=float)
    parser.add_argument("-mml", "--moment-matching-lag", default=1, type=int)
    parser.add_argument("-v", "--verbose", action='store_true')
    parser.add_argument('-ld', '--log-dir', type=str, default="./")
    parser.add_argument('-dd', '--data-dir', type=str, default="./")
    parser.add_argument("--plot", action='store_true')
    parser.add_argument("--fon", action='store_true')
    parser.add_argument("--ftm", action='store_true')
    parser.add_argument("--fom", action='store_true')
    parser.add_argument("--ftn", action='store_true')
    parser.add_argument("--load", action='store_true')
    parser.add_argument("--save", action='store_true')
    args = parser.parse_args()

    main(**vars(args))