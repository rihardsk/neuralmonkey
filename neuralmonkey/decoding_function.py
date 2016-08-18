import tensorflow as tf

from neuralmonkey.logging import debug

def attention_decoder(decoder_inputs, initial_state, attention_objects,
                      cell, loop_function=None, dtype=tf.float32, scope=None):

    output_size = cell.output_size
    maxout_size = 150

    outputs = []
    states = []

    #### WTF does this do?
    # do manualy broadcasting of the initial state if we want it
    # to be the same for all inputs
    if len(initial_state.get_shape()) == 1:
        debug("Warning! I am doing this weird job.")
        state_size = initial_state.get_shape()[0].value
        initial_state = tf.reshape(tf.tile(initial_state,
                                           tf.shape(decoder_inputs[0])[:1]),
                                   [-1, state_size])

    with tf.variable_scope(scope or "attention_decoder"):

        output, state = decode_step(decoder_inputs[0], initial_state,
                                    attention_objects, cell,
                                    maxout_size, output_size)
        outputs.append(output)
        states.append(state)

        for step in range(1, len(decoder_inputs)):
            tf.get_variable_scope().reuse_variables()

            if loop_function:
                decoder_input = loop_function(state, step)
            else:
                decoder_input = decoder_inputs[step]

            output, state = decode_step(decoder_input, state, attention_objects,
                                        cell, maxout_size, output_size)
            outputs.append(output)
            states.append(state)

    return outputs, states


def decode_step(prev_output, prev_state, attention_objects, rnn_cell,
                maxout_size, output_size):
    ## compute c_i:
    contexts = [a.attention(prev_state) for a in attention_objects]

    ## compute prob(y_i | s_i-1, y_i-1, c_i)
    output = maxout_projection(prev_state, prev_output, contexts,
                               maxout_size, output_size)

    ## compute s_i based on y_i-1, c_i and s_i-1
    _, state = rnn_cell(tf.concat(1, [prev_output] + contexts), prev_state)

    return output, state


def linear_projection(states, size):
    with tf.variable_scope("AttnOutputProjection"):
        output = tf.nn.seq2seq.linear(states, size, True)
        return output


def maxout_projection(prev_state, prev_output, current_contexts,
                      maxout_size, output_size):
    # in Bahdanau: t_i = [max(t'_{i,2j-1}, t'_{i,2j})] ... j = 1,...,l
    # where: t' is projection

    with tf.variable_scope("AttnMaxoutProjection"):
        input_ = [prev_state, prev_output] + current_contexts
        projected = tf.nn.seq2seq.linear(input_, maxout_size * 2, True)

        ## dropouts?!

        # projected je batch x (2*maxout_size)
        # potreba narezat po dvojicich v dimenzi jedna
        # aby byl batch x 2 x maxout_size

        maxout_input = tf.reshape(projected, [-1, 1, 2, maxout_size])

        maxpooled = tf.nn.max_pool(
            maxout_input, [1, 1, 2, 1], [1, 1, 2, 1], "SAME")

        reshaped = tf.reshape(maxpooled, [-1, maxout_size])
        return linear_projection(reshaped, output_size)


class Attention(object):
    def __init__(self, attention_states, scope, dropout_placeholder,
                 input_weights=None, max_fertility=None):
        self.scope = scope
        self.attentions_in_time = []
        self.attention_states = tf.nn.dropout(attention_states,
                                              dropout_placeholder)
        self.input_weights = input_weights

        with tf.variable_scope(scope):
            self.attn_length = attention_states.get_shape()[1].value
            self.attn_size = attention_states.get_shape()[2].value

            # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape
            # before.
            self.att_states_reshaped = tf.reshape(
                self.attention_states,
                [-1, self.attn_length, 1, self.attn_size])

            self.attention_vec_size = self.attn_size    # Size of query vectors
                                                        # for attention.

            k = tf.get_variable(
                "AttnW",
                [1, 1, self.attn_size, self.attention_vec_size])

            self.hidden_features = tf.nn.conv2d(self.att_states_reshaped, k,
                                                [1, 1, 1, 1], "SAME")

            self.v = tf.get_variable("AttnV", [self.attention_vec_size])

    def attention(self, query_state):
        """Put attention masks on att_states_reshaped
           using hidden_features and query.
        """

        with tf.variable_scope(self.scope+"/Attention"):
            y = tf.nn.seq2seq.linear(query_state, self.attention_vec_size, True)
            y = tf.reshape(y, [-1, 1, 1, self.attention_vec_size])

            s = self.get_logits(y)

            if self.input_weights is None:
                a = tf.nn.softmax(s)
            else:
                a_all = tf.nn.softmax(s) * self.input_weights
                norm = tf.reduce_sum(a_all, 1, keep_dims=True) + 1e-8
                a = a_all / norm

            self.attentions_in_time.append(a)

            # Now calculate the attention-weighted vector d.
            d = tf.reduce_sum(tf.reshape(a, [-1, self.attn_length, 1, 1])
                              * self.att_states_reshaped, [1, 2])

            return tf.reshape(d, [-1, self.attn_size])

    def get_logits(self, y):
        # Attention mask is a softmax of v^T * tanh(...).
        return tf.reduce_sum(self.v * tf.tanh(self.hidden_features + y), [2, 3])


class CoverageAttention(Attention):
    def __init__(self, attention_states, scope, dropout_placeholder,
                 input_weights=None, max_fertility=5):

        super(CoverageAttention, self).__init__(attention_states, scope,
                                                dropout_placeholder,
                                                input_weights=input_weights,
                                                max_fertility=max_fertility)

        self.coverage_weights = tf.get_variable("coverage_matrix",
                                                [1, 1, 1, self.attn_size])
        self.fertility_weights = tf.get_variable("fertility_matrix",
                                                 [1, 1, self.attn_size])
        self.max_fertility = max_fertility

        self.fertility = 1e-8 + self.max_fertility * tf.sigmoid(
            tf.reduce_sum(self.fertility_weights * self.attention_states, [2]))


    def get_logits(self, y):
        coverage = sum(
            self.attentions_in_time) / self.fertility * self.input_weights

        logits = tf.reduce_sum(
            self.v * tf.tanh(
                self.hidden_features + y + self.coverage_weights * tf.reshape(
                    coverage, [-1, self.attn_length, 1, 1])),
            [2, 3])

        return logits
