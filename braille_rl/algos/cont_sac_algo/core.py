import numpy as np
import os
import tensorflow as tf
import json

def placeholder(dim=None):
    if isinstance(dim, (list,)):
        ph = tf.compat.v1.placeholder(dtype=tf.float32, shape=[None,*dim])
    else:
        ph = tf.compat.v1.placeholder(dtype=tf.float32, shape=(None,dim) if dim else (None,))
    return ph

def placeholders(*args):
    return [placeholder(dim) for dim in args]

def get_vars(scope):
    return [x for x in tf.global_variables() if scope in x.name]

def count_vars(scope):
    v = get_vars(scope)
    return sum([np.prod(var.shape.as_list()) for var in v])

def build_model(x, use_prev_a, prev_a, g,
                action_input=None,
                output_dim=None,
                input_dims=[100,100],
                conv_filters=(8, 16, 32, 32),
                dense_units=(512,),
                kernel_width=3,
                strides=1,
                pooling='max',
                pooling_width=2,
                pooling_strides=1,
                hidden_activation='relu',
                output_activation='linear',
                batch_norm=False,
                dropout=0.0
                ):

    num_conv_layers = len(conv_filters)
    num_dense_layers = len(dense_units)
    num_hidden_layers = num_conv_layers + num_dense_layers
    num_layers = num_hidden_layers + 1

    # Replicate default parameters across layers, if required
    pooling = (pooling,) * num_conv_layers
    pooling_width = (pooling_width,) * num_conv_layers
    pooling_strides = (pooling_strides,) * num_conv_layers
    hidden_activation = (hidden_activation,) * num_hidden_layers
    batch_norm = (batch_norm,) * num_layers
    dropout = (dropout,) * num_layers

    initializer = tf.compat.v1.initializers.variance_scaling(scale=1.0)

    # Convolutional base
    for i in range(num_conv_layers):

        x = tf.layers.conv2d(inputs=x,
                             filters=conv_filters[i],
                             kernel_size=(kernel_width[i], kernel_width[i]),
                             strides=(strides[i], strides[i]),
                             activation=hidden_activation[i],
                             kernel_initializer=initializer)

        if batch_norm[i]:
            x = tf.layers.batch_normalization(inputs=x)

        if pooling[i] == 'max':
            x = tf.layers.max_pooling2d(inputs=x,
                                        pool_size=(pooling_width[i], pooling_width[i]),
                                        strides=(pooling_strides[i], pooling_strides[i]))
        elif pooling[i] == 'avg':
            x = tf.layers.average_pooling2d(inputs=x,
                                        pool_size=(pooling_width[i], pooling_width[i]),
                                        strides=(pooling_strides[i], pooling_strides[i]))

    # Dense layers
    x = tf.layers.flatten(inputs=x)

    # Concat in onehot goal array
    x = tf.concat([x, g], axis=-1)

    # concat previos action
    if use_prev_a:
        x = tf.concat([x, prev_a], axis=-1)

    # concat the action or policy into the network before fully connected layers for Q(s,a) values
    if action_input is not None:
        x = tf.concat([x, action_input], axis=-1)

    for i, j in enumerate(range(num_conv_layers, num_hidden_layers)):

        x = tf.layers.dense(inputs=x,
                            units=dense_units[i],
                            activation=hidden_activation[j],
                            kernel_initializer=initializer)

        if dropout[j] > 0.0:
            x = tf.layers.dropout(inputs=x,
                                  rates=dropout[j])

    # add output layer
    if output_dim is not None:
        x = tf.layers.dense(inputs=x,
                            units=output_dim,
                            activation=output_activation)

    return x

def clip_but_pass_gradient(x, l=-1., u=1.):
    clip_up = tf.cast(x > u, tf.float32)
    clip_low = tf.cast(x < l, tf.float32)
    return x + tf.stop_gradient((u - x)*clip_up + (l - x)*clip_low)

EPS = 1e-8
def gaussian_likelihood(x, mu, log_std):
    pre_sum = -0.5 * (((x-mu)/(tf.exp(log_std)+EPS))**2 + 2*log_std + np.log(2*np.pi))
    return tf.reduce_sum(pre_sum, axis=1)

LOG_STD_MAX = 2
LOG_STD_MIN = -20

def gaussian_policy(x, use_prev_a, prev_a, g, network_params):

    act_dim = prev_a.shape.as_list()[-1]

    net = build_model(x, use_prev_a, prev_a, g,
                    action_input=None,
                    output_dim=None,
                    **network_params)

    mu      = tf.layers.dense(net, act_dim, activation='linear')
    log_std = tf.layers.dense(net, act_dim, activation=tf.tanh)
    log_std = LOG_STD_MIN + 0.5 * (LOG_STD_MAX - LOG_STD_MIN) * (log_std + 1)

    std = tf.exp(log_std)
    pi = mu + tf.random_normal(tf.shape(mu)) * std
    logp_pi = gaussian_likelihood(pi, mu, log_std)
    return mu, pi, logp_pi

def apply_squashing_func(mu, pi, logp_pi):
    mu = tf.tanh(mu)
    pi = tf.tanh(pi)
    # To avoid evil machine precision error, strictly clip 1-pi**2 to [0,1] range.
    logp_pi -= tf.reduce_sum(tf.log(clip_but_pass_gradient(1 - pi**2, l=0, u=1) + 1e-6), axis=1)
    return mu, pi, logp_pi

def create_rl_networks(x, a, use_prev_a, prev_a, g, action_scale, network_params):

    # policy
    with tf.variable_scope('pi'):
        mu, pi, logp_pi = gaussian_policy(x, use_prev_a, prev_a, g, network_params)
        mu, pi, logp_pi = apply_squashing_func(mu, pi, logp_pi)

    # make sure actions are in correct range
    mu *= action_scale
    pi *= action_scale

    # vfs
    with tf.variable_scope('q1'):
        q1    = tf.squeeze(build_model(x, use_prev_a, prev_a, g,
                                     action_input=a,
                                     output_dim=1,
                                     **network_params), axis=1)


    with tf.variable_scope('q2'):
        q2    = tf.squeeze(build_model(x, use_prev_a, prev_a, g,
                                     action_input=a,
                                     output_dim=1,
                                     **network_params), axis=1)

    return mu, pi, logp_pi, q1, q2
