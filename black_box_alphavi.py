from __future__ import absolute_import
from __future__ import print_function
import autograd.numpy as np
import autograd.numpy.random as npr
import autograd.scipy.stats.norm as norm
from autograd.scipy.special import logsumexp
from autograd import grad
from autograd.util import quick_grad_check

import pandas as pd

import sys

import math

class WeightsParser(object):
    """A helper class to index into a parameter vector."""
    def __init__(self):
        self.idxs_and_shapes = {}
        self.num_weights = 0

    def add_shape(self, name, shape):
        start = self.num_weights
        self.num_weights += np.prod(shape)
        self.idxs_and_shapes[ name ] = (slice(start, self.num_weights), shape)

    def get(self, vect, name):
        idxs, shape = self.idxs_and_shapes[name]
        return np.reshape(vect[ idxs ], shape)

    def get_indexes(self, vect, name):
        idxs, shape = self.idxs_and_shapes[name]
        return idxs

def make_functions(d, shapes, alpha):
    N = sum((m + 1) * n for m, n in shapes)
    parser = WeightsParser()
    parser.add_shape('mean', (N, 1))
    parser.add_shape('log_variance', (N, 1))
    parser.add_shape('log_v_noise', (1, 1))

    w = 0.1 * np.random.randn(parser.num_weights)
    w[ parser.get_indexes(w, 'log_variance') ] = w[ parser.get_indexes(w, 'log_variance') ] - 10.0
    w[ parser.get_indexes(w, 'log_v_noise') ] = np.log(1.0)

    def predict(samples_q, X):

        # First layer

        K = samples_q.shape[ 0 ]
        (m, n) = shapes[ 0 ]
        W = samples_q[ : , : m * n ].reshape((n * K, m)).T
        b = samples_q[ : , m * n : m * n + n ].reshape(1, n * K)
        a = np.dot(X, W) + b
        h = np.maximum(a, 0)

        # Second layer

        samples_q = samples_q[ : , m * n + n : ]
        (m, n) = shapes[ 1 ]
        b = samples_q[ : , m * n : m * n + n ].T
        a = np.sum((samples_q[ : , : m * n ].reshape(1, -1) * h).reshape((K * X.shape[ 0 ], m)),
                   1).reshape((X.shape[ 0 ], K)) + b

        return a

    def log_likelihood_factor(samples_q, v_noise, X, y):
        outputs = predict(samples_q, X)
        log_p = -0.5 * np.log(2 * math.pi * v_noise) - 0.5 * (np.tile(y, (1, samples_q.shape[ 0 ])) - outputs)**2 / v_noise
        return np.mean(log_p, 0)
        
    def log_q(samples_q, q):
        log_q = -0.5 * np.log(2 * math.pi * q[ 'v' ]) - 0.5 * (samples_q - q[ 'm' ]) **2 / q[ 'v' ]
        return np.sum(log_q, 1)
    
    def log_prior(samples_q, v_prior):
        log_p0 = -0.5 * np.log(2 * math.pi * v_prior) - 0.5 * samples_q **2 / v_prior
        return np.sum(log_p0, 1)

    def draw_samples(q, K):
        return npr.randn(K, len(q[ 'm' ])) * np.sqrt(q[ 'v' ]) + q[ 'm' ]

    def get_parameters_q(w, v_prior, scale = 1.0):
        v = 1.0 / (scale * np.exp(-parser.get(w, 'log_variance'))[ :, 0 ] + 1.0 / v_prior)
        m = scale * parser.get(w, 'mean')[ :, 0 ] * np.exp(-parser.get(w, 'log_variance')[ :, 0 ]) * v
        return { 'm': m, 'v': v }

    def energy(w, X, y, v_prior, K, N, is_max = "no"):
        q = get_parameters_q(w, v_prior)
        v_noise = np.exp(parser.get(w, 'log_v_noise')[ 0, 0 ])
        samples_q = draw_samples(q, K)
        log_factor_value = 1.0 * N * log_likelihood_factor(samples_q, v_noise, X, y)
        
        if is_max == "yes":
            logp0 = log_prior(samples_q, v_prior)
            logq = log_q(samples_q, q)
            logF = logp0 + log_factor_value - logq
            vfe = -np.max(logF)
        elif alpha > 1.0 - 10e-5 and alpha < 1.0 + 10e-5:
            KL = np.sum(-0.5 * np.log(2 * math.pi * v_prior) - 0.5 * (q[ 'm' ]**2 + q[ 'v' ]) / v_prior) - \
                np.sum(-0.5 * np.log(2 * math.pi * q[ 'v' ] * np.exp(1)))
            vfe = -(np.mean(log_factor_value) + KL)
        else:
            logp0 = log_prior(samples_q, v_prior)
            logq = log_q(samples_q, q)
            logF = logp0 + log_factor_value - logq
            logF = (1 - alpha) * logF
            vfe = -(logsumexp(logF) - np.log(K))            
            vfe = vfe / (1 - alpha)
            
        return vfe

    def get_error_and_ll(w, v_prior, X, y, K, location, scale):
        v_noise = np.exp(parser.get(w, 'log_v_noise')[ 0, 0 ]) * scale**2
        q = get_parameters_q(w, v_prior)
        samples_q = draw_samples(q, K)
        outputs = predict(samples_q, X) * scale + location
        log_factor = -0.5 * np.log(2 * math.pi * v_noise) - 0.5 * (np.tile(y, (1, K)) - np.array(outputs))**2 / v_noise
        ll = np.mean(logsumexp(log_factor - np.log(K), 1))
        error = np.sqrt(np.mean((y - np.mean(outputs, 1, keepdims = True))**2))
        return error, ll

    def update_v_prior(w, v_prior):
        q = get_parameters_q(w, v_prior)
        return np.mean(q[ 'm' ]**2 + q[ 'v' ])

    return w, energy, update_v_prior, get_error_and_ll

def make_batches(N_data, batch_size):
    return [ slice(i, min(i + batch_size, N_data)) for i in range(0, N_data, batch_size) ]

def fit_q(X, y, hidden_layer_size, batch_size, epochs, K, alpha = 1.0, learning_rate = 1e-2, v_prior = 1.0):

    hidden_layer_size = [ hidden_layer_size ]
    hidden_layer_size = np.array([ X.shape[ 1 ] ] + hidden_layer_size + [ 1 ])
    shapes = list(zip(hidden_layer_size[ : -1 ], hidden_layer_size[ 1 : ]))
    w, energy, update_v_prior, get_error_and_ll = make_functions(X.shape[ 1 ], shapes, alpha)
    energy_grad = grad(energy)

    # Check the gradients numerically, just to be safe

    #quick_grad_check(energy, w, (X, y, v_prior, K, X.shape[ 0 ]))

    #print("    Epoch      |    Error  |   Log-likelihood  ")

    def print_perf(epoch, w):
        error, ll = get_error_and_ll(w, v_prior, X, y, K, 0.0, 1.0)
        print("{0:15}|{1:15}|{2:15}".format(epoch, error, ll))
        sys.stdout.flush()

    # Train with sgd

    batch_idxs = make_batches(X.shape[0], batch_size)

    m1 = 0
    m2 = 0
    beta1 = 0.9
    beta2 = 0.999
    epsilon = 1e-8
    t = 0

    for epoch in range(epochs):
        permutation = np.random.choice(range(X.shape[ 0 ]), X.shape[ 0 ], replace = False)
        #print_perf(epoch, w)
        for idxs in batch_idxs:
            t += 1
            if alpha > -10**5:
                grad_w = energy_grad(w, X[ permutation[ idxs ] ], y[ permutation[ idxs ] ], v_prior, K, X.shape[ 0 ])
            else:
                grad_w = energy_grad(w, X[ permutation[ idxs ] ], y[ permutation[ idxs ] ], v_prior, K, X.shape[ 0 ], is_max = "yes")
            m1 = beta1 * m1 + (1 - beta1) * grad_w
            m2 = beta2 * m2 + (1 - beta2) * grad_w**2
            m1_hat = m1 / (1 - beta1**t)
            m2_hat = m2 / (1 - beta2**t)
            w -= learning_rate * m1_hat / (np.sqrt(m2_hat) + epsilon)
  

    return w, v_prior, get_error_and_ll
