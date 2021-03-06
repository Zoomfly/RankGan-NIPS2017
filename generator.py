import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops


class Generator(object):
    def __init__(self, opt, FLAGS, pretrain = True):

        self.num_emb = opt.vocab_size
        self.emb_dim = opt.g_emb_dim
        self.hidden_dim = opt.g_hid_dim
        self.seq_len = opt.seq_len
        self.pre_learning_rate = FLAGS.pre_g_lr
        self.learning_rate = FLAGS.ad_g_lr
        if pretrain:
            self.batch_size = FLAGS.gen_pre_batch_size
        else:
            self.batch_size = FLAGS.gen_batch_size
        self.start_token = tf.constant([opt.start_token] * self.batch_size, dtype=tf.int32)

        self.g_params = []
        self.d_params = []
        self.grad_clip = 5.0
        self.LSTM_initialization()
        self.generation_graph()
        self.prediction_graph()
        self.mle_pretrain()
        self.adversarial_train()


    def LSTM_initialization(self):
        # construct LSTMs
        with tf.variable_scope('generator'):
            self.g_embeddings = tf.Variable(self.init_matrix([self.num_emb, self.emb_dim]))
            self.g_params.append(self.g_embeddings)
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.create_output_unit(self.g_params)  # maps h_t to o_t (output token logits)

        # placeholder definition
        self.x = tf.placeholder(tf.int32, shape=[self.batch_size, self.seq_len]) # sequence of tokens generated by generator
        self.rewards = tf.placeholder(tf.float32, shape=[self.batch_size, self.seq_len]) # get from rollout policy and ranker

        # word embedding
        with tf.device("/cpu:0"):
            self.processed_x = tf.transpose(tf.nn.embedding_lookup(self.g_embeddings, self.x), perm=[1, 0, 2])  # seq_length x batch_size x emb_dim

        # Zero states
        self.h0 = tf.zeros([self.batch_size, self.hidden_dim])
        self.h0 = tf.stack([self.h0, self.h0])

    def generation_graph(self):
        gen_x = []
        with tf.variable_scope("LSTM_sampling"):
            tf.get_variable_scope().reuse_variables()
            # the first step
            h_t = self.g_recurrent_unit(tf.nn.embedding_lookup(self.g_embeddings, self.start_token), self.h0)
            o_t = self.g_output_unit(h_t)
            for i in range(self.seq_len):
                log_prob = tf.log(tf.nn.softmax(o_t))
                next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)
                x_tp1 = tf.nn.embedding_lookup(self.g_embeddings, next_token)  # batch x emb_dim

                h_t = self.g_recurrent_unit(x_tp1, h_t)
                o_t = self.g_output_unit(h_t)
                gen_x.append(next_token)

        self.gen_x = tf.transpose(tf.stack(gen_x), (1, 0))  # batch_size x seq_length


    def prediction_graph(self):
        # predictions = []
        predictions = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.seq_len,
            dynamic_size=False, infer_shape=True)

        # ta_emb_x = tensor_array_ops.TensorArray(
        #     dtype=tf.float32, size=self.seq_len)
        # ta_emb_x = ta_emb_x.unstack(self.processed_x)
        with tf.variable_scope("LSTM_training"):
            for i in range(self.seq_len):
                tf.get_variable_scope().reuse_variables()
                if i == 0:
                    # the first step
                    h_t = self.g_recurrent_unit(tf.nn.embedding_lookup(self.g_embeddings, self.start_token), self.h0)
                    o_t = self.g_output_unit(h_t)
                else:
                    # tf.get_variable_scope().reuse_variables()
                    # x_tp1 = ta_emb_x.read(i-1)
                    # h_t = self.g_recurrent_unit(x_tp1, h_t)
                    h_t = self.g_recurrent_unit(self.processed_x[i-1, :, :], h_t)
                    o_t = self.g_output_unit(h_t)
                    #############
                target_logit = o_t
                cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(logits = target_logit,
                                labels = self.x[:, i])
                predictions = predictions.write(i, tf.clip_by_value(cross_entropy, 0.0, 45.0))
                # predictions.append(tf.clip_by_value(cross_entropy, 0.0, 45.0))

        self.predictions = tf.reshape(predictions.stack(), [-1])
        # self.predictions = tf.reshape(predictions, [-1])


    #######################################################################################################
    #  MLE pretraining
    #######################################################################################################
    def mle_pretrain(self):
        self.pretrain_loss = tf.reduce_sum(self.predictions) / (self.seq_len * self.batch_size)
        # training updates
        pretrain_opt = self.g_optimizer(self.pre_learning_rate)

        self.pretrain_grad, _ = tf.clip_by_global_norm(tf.gradients(self.pretrain_loss, self.g_params), self.grad_clip)
        self.pretrain_updates = pretrain_opt.apply_gradients(zip(self.pretrain_grad, self.g_params))

    #######################################################################################################
    #  Adversarial training
    #######################################################################################################
    def adversarial_train(self):
        self.g_loss = tf.reduce_sum(self.predictions * tf.reshape(self.rewards, [-1]))
        g_opt = self.g_optimizer(self.learning_rate)
        self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params), self.grad_clip)
        self.g_updates = g_opt.apply_gradients(zip(self.g_grad, self.g_params))


    ########################################## 
    ##########################################
    ##########################################
    #basic models for LSTMs
    def generate(self, sess):
        outputs = sess.run(self.gen_x)
        return outputs

    def pretrain_step(self, sess, x):
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss], feed_dict={self.x: x})
        return outputs

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=0.1)

    def init_vector(self, shape):
        return tf.zeros(shape)

    def create_recurrent_unit(self, params):
        # Weights and Bias for input and hidden tensor
        self.Wi = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Ui = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bi = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wf = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uf = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bf = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wog = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uog = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bog = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wc = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uc = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bc = tf.Variable(self.init_matrix([self.hidden_dim]))
        params.extend([
            self.Wi, self.Ui, self.bi,
            self.Wf, self.Uf, self.bf,
            self.Wog, self.Uog, self.bog,
            self.Wc, self.Uc, self.bc])

        def unit(x, hidden_memory_tm1):
            previous_hidden_state, c_prev = tf.unstack(hidden_memory_tm1)

            # Input Gate
            i = tf.sigmoid(
                tf.matmul(x, self.Wi) +
                tf.matmul(previous_hidden_state, self.Ui) + self.bi
            )

            # Forget Gate
            f = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                tf.matmul(previous_hidden_state, self.Uf) + self.bf
            )

            # Output Gate
            o = tf.sigmoid(
                tf.matmul(x, self.Wog) +
                tf.matmul(previous_hidden_state, self.Uog) + self.bog
            )

            # New Memory Cell
            c_ = tf.nn.tanh(
                tf.matmul(x, self.Wc) +
                tf.matmul(previous_hidden_state, self.Uc) + self.bc
            )

            # Final Memory cell
            c = f * c_prev + i * c_

            # Current Hidden state
            current_hidden_state = o * tf.nn.tanh(c)

            return tf.stack([current_hidden_state, c])

        return unit

    def create_output_unit(self, params):
        self.Wo = tf.Variable(self.init_matrix([self.hidden_dim, self.num_emb]))
        self.bo = tf.Variable(self.init_matrix([self.num_emb]))
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            hidden_state, c_prev = tf.unstack(hidden_memory_tuple)
            # hidden_state : batch x hidden_dim
            logits = tf.matmul(hidden_state, self.Wo) + self.bo
            # output = tf.nn.softmax(logits)
            return logits

        return unit

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)
