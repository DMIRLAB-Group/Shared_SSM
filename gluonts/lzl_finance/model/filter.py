import tensorflow as tf
import tensorflow_probability as tfp
import numpy as np


class MultiKalmanFilter(object):
    """
    This class defines a Kalman Filter (Linear Gaussian State Space model), possibly with a dynamics parameter
    network alpha.
    """

    def __init__(self, dim_l, dim_z, dim_u=0, dim_k=1, **kwargs):

        self.dim_l = dim_l
        self.dim_z = dim_z  #stands for the target observation
        self.dim_u = dim_u  #stands for the enviroment input
        self.dim_k = dim_k  #stands number of the global transition

        # Initializer for identity matrix
        self.eye_init = lambda shape, dtype=np.float32: np.eye(*shape, dtype=dtype)

        # Pop all variables
        # 原代码 pop 初始化的维度都不对，所以删除
        init = kwargs.pop('mu', None)
        self.mu = tf.get_variable('mu', initializer=init, trainable=False)  # state

        init = kwargs.pop('Sigma',None).astype(np.float32)
        self.Sigma = tf.get_variable('Sigma', initializer=init, trainable=False)  # uncertainty covariance

        init = kwargs.pop('z_0',None).astype(np.float32)
        self.z_0 = tf.get_variable('z_0', initializer=init)  # initial output

        init = kwargs.pop('A', None)
        self.A = tf.get_variable('A', initializer=init)

        init = kwargs.pop('B', None).astype(np.float32)
        self.B = tf.get_variable('B', initializer=init)  # control transition matrix

        init = kwargs.pop('Q', None)
        self.Q = tf.get_variable('Q', initializer=init, trainable=False)  # process uncertainty

        init = kwargs.pop('C', None).astype(np.float32)
        self.C = tf.get_variable('C', initializer=init)   # Measurement function

        init = kwargs.pop('R', None)
        self.R = tf.get_variable('R', initializer=init, trainable=False)   # state uncertainty

        self._alpha_sq = tf.constant(1., dtype=tf.float32) # fading memory control
        self.M = 0              # process-measurement cross correlation

        # identity matrix
        self._I = tf.constant(self.eye_init((dim_l, dim_l)), name='I')

        # Get variables that are possibly defined with tensors
        self.z = kwargs.pop('z', None)
        if self.z is None:
            self.z = tf.placeholder(tf.float32, shape=(None ,None, None, dim_z), name='z')

        self.z_scale = kwargs.pop('z_scale' , None)
        if self.z_scale is None:
            self.z_scale = tf.placeholder(tf.float32 , shape=(None ,None,dim_z) , name = 'z_scale')

        self.u = kwargs.pop('u', None)
        if self.u is None:
            self.u = tf.placeholder(tf.float32, shape=(None, None, dim_u), name='u')

        self.mask = kwargs.pop('mask', None)
        if self.mask is None:
            self.mask = tf.placeholder(tf.float32, shape=(None, None), name='mask')

        self.alpha_fn = kwargs.pop('alpha', None)
        self.state = kwargs.pop('state', None)
        self.log_likelihood = None

    def forward_step_fn(self, params, inputs):
        """
        使用 tf.scan 完成 一步 Filter 算法
        输入： p(l_t|z_(1:t-1)) , z_t
        输出： p(l_t|z_(1:t)) , p(l_(t+1) | z_(1:t))
        """
        mu_pred, Sigma_pred, _, _, alpha, u, state, _, _, _ = params
        valueInputs , Q , R = inputs #(ssm_num, bs  ,2+dim_u) , (bs ,dim_l ,dim_l)
        z = tf.slice(valueInputs, [0,0,0], [-1,-1, self.dim_z])  # (ssm_num , bs, dim_z)
        _u = tf.slice(valueInputs, [0,0, self.dim_z], [-1,-1, self.dim_u])  # (ssm_num ,bs, dim_u)
        mask = tf.slice(valueInputs, [0,0, self.dim_z + self.dim_u], [-1,-1, 1])  # (ssm_num , bs, 1)

        # Mixture of C
        C = tf.matmul(alpha, tf.reshape(self.C, [alpha.shape[0],-1, self.dim_z * self.dim_l]))  # (ssm_num ,bs, k) x (ssm_num , k , dim_z_ob*dim_z)
        C = tf.reshape(C, [alpha.shape[0],alpha.shape[1], self.dim_z, self.dim_l])  # (ssm_num , bs, dim_z, dim_l)

        # Residual
        z_pred = tf.squeeze(tf.matmul(C, tf.expand_dims(mu_pred, -1)),axis=-1)  # (ssm_num , bs, dim_z)
        r = z - z_pred  # (ssm_num ,bs, dim_z)

        # R的维度(bs,dim_z,dim_z) C 的维度(ssm_num , bs , dim_z , dim_l)
        S = tf.matmul(tf.matmul(C, Sigma_pred), C, transpose_b=True) + R  # (ssm_num , bs, dim_z, dim_z)

        S_inv = tf.matrix_inverse(S)
        K = tf.matmul(tf.matmul(Sigma_pred, C, transpose_b=True), S_inv)  # (ssm_num ,bs, dim_l, dim_z)

        # For missing values, set to 0 the Kalman gain matrix
        # multiply 这个方法是从高维开始对齐的
        K = tf.multiply(tf.expand_dims(mask, -1), K) #(ssm_num , bs, dim_l , dim_z)

        # Get current mu and Sigma
        mu_t = mu_pred + tf.squeeze(tf.matmul(K, tf.expand_dims(r, -1)))  # (ssm_num , bs, dim_l)
        I_KC = self._I - tf.matmul(K, C)  # (ssm_num , bs, dim_l, dim_l)
        # TODO: 这里的 Sigma_t 的计算方式不太一样
        Sigma_t = tf.matmul(tf.matmul(I_KC, Sigma_pred), I_KC, transpose_b=True) + self._sast(R, K)  # (ssm_num , bs, dim_l, dim_l)

        # Mixture of A
        alpha, state, u = self.alpha_fn(tf.multiply(mask, z) + tf.multiply((1 - mask), z_pred), state, _u, reuse=True)  # (ssm_num ,bs, k)
        A = tf.matmul(alpha, tf.reshape(self.A, [-1,alpha.shape[-1], self.dim_l * self.dim_l]))  # (ssm_num ,bs, k) x (ssm_num ,k, dim_l*dim_l)
        A = tf.reshape(A, [alpha.shape[0], alpha.shape[1], self.dim_l, self.dim_l])  # (bs, dim_l, dim_l)
        A.set_shape(Sigma_pred.get_shape())  # ( ssm_num , batch_size , dim_l , dim_l)

        # Mixture of B
        B = tf.matmul(alpha, tf.reshape(self.B, [-1, alpha.shape[-1], self.dim_l * self.dim_u]))  # (ssm_num ,bs, k) x (ssm_num ,k, dim_z*dim_l)
        B = tf.reshape(B, [alpha.shape[0],alpha.shape[1], self.dim_l, self.dim_u])  # (ssm_num , bs, dim_z, dim_l)
        B.set_shape([A.get_shape()[0], A.get_shape()[1], self.dim_l, self.dim_u])

        # Prediction
        mu_pred = tf.squeeze(tf.matmul(A, tf.expand_dims(mu_t, -1)) ,-1) + tf.squeeze(tf.matmul(B, tf.expand_dims(u, -1)) ,-1) #(ssm_num , bs , dim_l)
        Sigma_pred = tf.scalar_mul(self._alpha_sq, tf.matmul(tf.matmul(A, Sigma_t), A, transpose_b=True) + Q) #(ssm_num ,bs, dim_l, dim_l )

        return mu_pred, Sigma_pred, mu_t, Sigma_t, alpha, u, state, A, B, C

    def backward_step_fn(self, params, inputs):
        """
        使用 tf.scan 完成 一步 Smoother 算法
        输入： p(l_t|z_(1:t) , u_(1:t))  t = T,...,2
              p(l_t | l_(t-1) )  t= T,...2
        输出： p(l_t|z_(1:T))
        """
        mu_back, Sigma_back = params
        mu_pred_tp1, Sigma_pred_tp1, mu_filt_t, Sigma_filt_t, A = inputs

        J_t = tf.matmul(tf.transpose(A, [0,1,3,2]), tf.matrix_inverse(Sigma_pred_tp1)) # (ssm_num , bs , dim_l , dim_l) x (ssm_num , bs ,dim_l , dim_l)
        J_t = tf.matmul(Sigma_filt_t, J_t)

        mu_back = mu_filt_t + tf.matmul(J_t, mu_back - mu_pred_tp1)
        Sigma_back = Sigma_filt_t + tf.matmul(J_t, tf.matmul(Sigma_back - Sigma_pred_tp1, J_t, adjoint_b=True))

        return mu_back, Sigma_back

    def compute_forwards(self, reuse=None):
        """
        计算 Kalman Filter 的前向
        是一种 on_line 设置
        # 注意这里的 bs 前面都多了一个 ssm_num 的维度，已经增加
        输入: 初始状态 l_0 , z_(1:T)
        输出： p(l_t | l_(t-1) , u_t) , t= 2,...,T+1
              p(l_t | x_(1:t) , u_(1:t)) , t = 1,...,T
        """

        # To make sure we are not accidentally using the real outputs in the steps with missing values, set them to 0.
        z_masked = tf.multiply(self.mask, self.z) #(ssm_num , bs, seq ,1)
        inputs = [tf.transpose(tf.concat([z_masked, self.u, self.mask], axis=-1),[2,0,1,3]), #(seq , ssm_num , bs ,1+dim_u+1)
                  tf.transpose(self.Q , [1,0,2,3]),
                  tf.transpose(self.R , [1,0,2,3])
                  ]

        z_prev = tf.expand_dims(self.z_0, 1)  # (ssm_num,1, 1)
        z_prev = tf.tile(z_prev, (1,tf.shape(self.mu)[1], 1)) #(ssm_num , bs , 1)
        alpha, state, u = self.alpha_fn(z_prev, self.state, self.u[:, :, 0], reuse= reuse)

        # 用于占位的矩阵 A B C
        dummy_init_A = tf.ones([self.Sigma.get_shape()[0] , self.Sigma.get_shape()[1], self.dim_l, self.dim_l])
        dummy_init_B = tf.ones([self.Sigma.get_shape()[0], self.Sigma.get_shape()[1], self.dim_l, self.dim_u])
        dummy_init_C = tf.ones([self.Sigma.get_shape()[0], self.Sigma.get_shape()[1], self.dim_z, self.dim_l])
        forward_states = tf.scan(self.forward_step_fn, inputs,
                                 initializer=(self.mu, self.Sigma, self.mu, self.Sigma, alpha, u, state,
                                              dummy_init_A, dummy_init_B, dummy_init_C),
                                 parallel_iterations=1, name='forward')
        return forward_states

    def compute_backwards(self, forward_states):
        '''
        计算 Kalman Smoother 的 后向操作
        是一种 off_line 的设置
        输入: 初始状态 p(l_T | z_(1:T), u_(1:T))
              p(l_t | l_(t-1) , u_t) , t= 2,...,T
              p(l_t | z_(1:t) , u_(1:t)) , t= 1,....,T
        输出： p(l_t | z_(1:T) , u_(1:T)) , t= 2,...,T

        '''
        mu_pred, Sigma_pred, mu_filt, Sigma_filt, alpha, u, state, A, B, C = forward_states
        mu_pred = tf.expand_dims(mu_pred, -1) #(seq , ssm_num , bs , dim_l ,1)
        mu_filt = tf.expand_dims(mu_filt, -1) #(seq , ssm_num , bs , dim_l , 1)
        states_scan = [mu_pred[:-1, :, :, : ,:],
                       Sigma_pred[:-1, :, :, : ,:],
                       mu_filt[:-1, :, :, : , :],
                       Sigma_filt[:-1, :, :, : , :],
                       A[:-1]]

        # 对 seq 维进行逆反
        dims = [0]
        for i, state in enumerate(states_scan):
            states_scan[i] = tf.reverse(state, dims)

        # Kalman Smoother
        # 给定 l_(T+1) | T 以及 l_(T) | T 最为初始状态
        backward_states = tf.scan(self.backward_step_fn, states_scan,
                                  initializer=(mu_filt[-1, :, :, :], Sigma_filt[-1, :, :, :]), parallel_iterations=1,
                                  name='backward')

        # 将时间维逆反回来
        backward_states = list(backward_states)
        dims = [0]
        for i, state in enumerate(backward_states):
            backward_states[i] = tf.reverse(state, dims)

        # 将 l_T | T 添加回来
        backward_states[0] = tf.concat([backward_states[0], mu_filt[-1:]], axis=0)
        backward_states[1] = tf.concat([backward_states[1], Sigma_filt[-1:]], axis=0) #(seq, ssm_num , bs , dim_z , dim_z)

        #将期望中多余的维度去除掉
        backward_states[0] = backward_states[0][... , 0] #(seq ,ssm_num, bs, dim_z)

        return backward_states, A, B, C, alpha

    def sample_generative_tf(self, backward_states, n_steps, deterministic=True, init_fixed_steps=1):
        """
        Get a sample from the generative model
        """
        # Get states from the Kalman filter to get the initial state
        mu_l, sigma_l = backward_states
        # l = tf.contrib.distributions.MultivariateNormalTriL(mu_l[seq_idx, 0], sigma_l[seq_idx, 0]).sample()

        if init_fixed_steps > 0:
            l = mu_l[:, 0]
            l = tf.expand_dims(l, 2)
        else:
            raise("Prior sampling from l not implemented")

        if not deterministic:
            # Pre-compute samples of noise
            noise_trans = tfp.distributions.MultivariateNormalTriL(tf.zeros((self.dim_l,)), tf.cholesky(self.Q))
            epsilon = noise_trans.sample((l.get_shape()[0].value, n_steps))
            noise_emiss = tfp.distributions.MultivariateNormalTriL(tf.zeros((self.dim_z,)), tf.cholesky(self.R))
            delta = noise_emiss.sample((l.get_shape()[0].value, n_steps))
        else:
            epsilon = tf.zeros((l.get_shape()[0], n_steps, self.dim_l))
            delta = tf.zeros((l.get_shape()[0], n_steps, self.dim_z))

        z_prev = tf.expand_dims(self.z_0, 0)  # (1, dim_z_ob)
        z_prev = tf.tile(z_prev, (tf.shape(self.mu)[0], 1))  # (bs, dim_z_ob)
        alpha, state, u = self.alpha_fn(z_prev, self.state, self.u[:, 0], reuse=True)

        z_samples = list()
        l_samples = list()
        alpha_samples = list()
        for n in range(n_steps):

            # Mixture of C
            C = tf.matmul(alpha, tf.reshape(self.C, [-1, self.dim_z * self.dim_l]))  # (bs, k) x (k, dim_z_ob*dim_z)
            C = tf.reshape(C, [-1, self.dim_z, self.dim_l])  # (bs, dim_z_ob, dim_z)

            # Output for the current time step
            y = tf.matmul(C, l) + tf.expand_dims(delta[:, n], 2)
            y = tf.squeeze(y, 2)

            # Store current state and output at time t
            l_samples.append(tf.squeeze(l, 2))
            z_samples.append(y)

            # Compute the mixture of A
            alpha, state, u = self.alpha_fn(y, state, self.u[:, n], reuse=True)
            alpha_samples.append(alpha)
            A = tf.matmul(alpha, tf.reshape(self.A, [-1, self.dim_l * self.dim_l]))
            A = tf.reshape(A, [-1, self.dim_l, self.dim_l])

            # Mixture of B
            B = tf.matmul(alpha, tf.reshape(self.B, [-1, self.dim_l * self.dim_u]))  # (bs, k) x (k, dim_z_ob*dim_z)
            B = tf.reshape(B, [-1, self.dim_l, self.dim_u])  # (bs, dim_z_ob, dim_l)

            # Get new state z_{t+1}
            # l = tf.matmul(A, l) + tf.matmul(B,  tf.expand_dims(self.u[:, n],2)) + tf.expand_dims(epsilon[:, n], 2)
            if (n + 1) >= init_fixed_steps:
                l = tf.matmul(A, l) + tf.matmul(B,  tf.expand_dims(u, 2)) + tf.expand_dims(epsilon[:, n], 2)
            else:
                l = mu_l[:, n+1]
                l = tf.expand_dims(l, 2)

        return tf.stack(z_samples, 1), tf.stack(l_samples, 1), tf.stack(alpha_samples, 1)

    def get_elbo(self, backward_states, A, B, C):

        mu_smooth = backward_states[0]  #(ssm_num ,bs ,seq , dim_l)
        Sigma_smooth = backward_states[1]  #(ssm_num , bs , seq , dim_l, dim_l)

        # 从 smooth 之后的分布进行采样
        mvn_smooth = tfp.distributions.MultivariateNormalTriL(mu_smooth, tf.cholesky(Sigma_smooth))
        z_smooth = mvn_smooth.sample() #(ssm_num ,bs , seq , dim_l)

        ## Transition distribution \prod_{t=2}^T p(z_t|z_{t-1}, u_{t})
        # We need to evaluate N(z_t; Az_(t-1) + Bu_t, Q), where Q is the same for all the elements
        Az_tm1 = tf.reshape(tf.matmul(A[:, :-1], tf.expand_dims(z_smooth[:, :-1], 3)), [-1, self.dim_l])

        # Remove the first input as our prior over z_1 does not depend on it
        # u_t_resh = tf.reshape(u, [-1, self.dim_u])
        # Bu_t = tf.transpose(tf.matmul(self.B, tf.transpose(u_t_resh)))
        Bu_t = tf.reshape(tf.matmul(B[:, :-1], tf.expand_dims(self.u[:, 1:], 3)), [-1, self.dim_l])
        mu_transition = Az_tm1 + Bu_t
        z_t_transition = tf.reshape(z_smooth[:, 1:, :], [-1, self.dim_l])

        # MultivariateNormalTriL supports broadcasting only for the inputs, not for the covariance
        # To exploit this we then write N(z_t; Az_tm1 + Bu_t, Q) as N(z_t - Az_tm1 - Bu_t; 0, Q)
        trans_centered = z_t_transition - mu_transition
        mvn_transition = tfp.distributions.MultivariateNormalTriL(tf.zeros(self.dim_l), tf.cholesky(self.Q))
        log_prob_transition = mvn_transition.log_prob(trans_centered)

        ## Emission distribution \prod_{t=1}^T p(y_t|z_t)
        # We need to evaluate N(y_t; Cz_t, R). We write it as N(y_t - Cz_t; 0, R)
        # z_t_emission = tf.reshape(z_smooth, [-1, self.dim_l])
        # Cz_t = tf.transpose(tf.matmul(self.C, tf.transpose(z_t_emission)))
        Cz_t = tf.reshape(tf.matmul(C, tf.expand_dims(z_smooth, 3)), [-1, self.dim_z])

        y_t_resh = tf.reshape(self.z, [-1, self.dim_z])
        emiss_centered = y_t_resh - Cz_t
        mvn_emission = tfp.distributions.MultivariateNormalTriL(tf.zeros(self.dim_z), tf.cholesky(self.R))
        mask_flat = tf.reshape(self.mask, (-1, ))
        log_prob_emission = mvn_emission.log_prob(emiss_centered)
        log_prob_emission = tf.multiply(mask_flat, log_prob_emission)

        ## Distribution of the initial state p(z_1|z_0)
        z_0 = z_smooth[:, 0, :]
        mvn_0 = tfp.distributions.MultivariateNormalTriL(self.mu, tf.cholesky(self.Sigma))
        log_prob_0 = mvn_0.log_prob(z_0)

        # Entropy log(\prod_{t=1}^T p(z_t|y_{1:T}, u_{1:T}))
        entropy = - mvn_smooth.log_prob(z_smooth)
        entropy = tf.reshape(entropy, [-1])
        # entropy = tf.zeros(())

        # Compute terms of the lower bound
        # We compute the log-likelihood *per frame*
        num_el = tf.reduce_sum(mask_flat)
        log_probs = [tf.truediv(tf.reduce_sum(log_prob_transition), num_el),
                     tf.truediv(tf.reduce_sum(log_prob_emission), num_el),
                     tf.truediv(tf.reduce_sum(log_prob_0), num_el),
                     tf.truediv(tf.reduce_sum(entropy), num_el)]

        kf_elbo = tf.reduce_sum(log_probs)

        return kf_elbo, log_probs, z_smooth

    def filter(self):
        mu_pred, Sigma_pred, mu_filt, Sigma_filt, alpha, u, state,  A, B, C = forward_states = \
            self.compute_forwards(reuse=True)
        forward_states = [mu_filt, Sigma_filt]
        # Swap batch dimension and time dimension
        forward_states[0] = tf.transpose(forward_states[0], [1, 0, 2])
        forward_states[1] = tf.transpose(forward_states[1], [1, 0, 2, 3])
        return tuple(forward_states), tf.transpose(A, [1, 2, 0, 3, 4]), tf.transpose(B, [1, 2, 0, 3, 4]), \
               tf.transpose(C, [1, 2, 0, 3, 4]), tf.transpose(alpha, [1, 2, 0, 3])

    def smooth(self):
        backward_states, A, B, C, alpha = self.compute_backwards(self.compute_forwards())
        # Swap batch dimension and time dimension
        backward_states[0] = tf.transpose(backward_states[0], [1, 2, 0, 3])
        backward_states[1] = tf.transpose(backward_states[1], [1, 2, 0, 3, 4])
        return tuple(backward_states), \
               tf.transpose(A, [1, 2, 0, 3, 4]),\
               tf.transpose(B, [1, 2, 0, 3, 4]),\
               tf.transpose(C, [1, 2, 0, 3, 4]),\
               tf.transpose(alpha, [1, 2, 0, 3])

    def _sast(self, R, K):
        '''
        :param R: (bs ,dim_z , dim_z)
        :param K: (ssm_num , bs , dim_l , dim_z)
        :return:
        '''
        ssm_dim , _ , dim_1, dim_2 = K.get_shape().as_list()
        R = tf.tile(tf.expand_dims(R, axis=0), [ssm_dim, 1, 1, 1]) #(ssm_num , bs , dim_z, dim_z)

        sast = tf.matmul(K, R, transpose_b=True) #(ssm_num ,bs , dim_l , dim_z)
        sast = tf.transpose(sast, [0,1, 3, 2]) #(ssm_num , bs , dim_z , dim_u)
        sast = tf.matmul(K, sast) #(ssm_num , bs ,dim_l , dim_l )
        return sast

