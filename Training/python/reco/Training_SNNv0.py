import os
import gc
import sys
import glob
import time
import math
import numpy as np
# import uproot
# import pandas
from functools import partial
from concurrent.futures import ThreadPoolExecutor

import tensorflow as tf
from tensorflow import keras
import tensorflow.keras.backend as K
from tensorflow.keras import regularizers
from tensorflow.keras.models import Sequential, Model, load_model
from tensorflow.keras.layers import Input, Dense, Conv2D, Dropout, AlphaDropout, Activation, BatchNormalization, Flatten, \
                                    Concatenate, PReLU, TimeDistributed, LSTM, Masking
from tensorflow.keras.callbacks import Callback, ModelCheckpoint, CSVLogger
from datetime import datetime

sys.path.insert(0, "..")
from commonReco import *
import DataLoader

# tf.debugging.experimental.enable_dump_debug_info(
#     "./debugv5_logdir",
#     tensor_debug_mode="FULL_HEALTH",
#     circular_buffer_size=-1)

class MyGNNLayer(tf.keras.layers.Layer):
    def __init__(self, n_dim, num_outputs, regu_rate, **kwargs):
        super(MyGNNLayer, self).__init__(**kwargs)
        self.n_dim        = n_dim
        self.num_outputs  = num_outputs
        self.supports_masking = True # to pass the mask to the next layers and not destroy it
        self.regu_rate = regu_rate

    def build(self, input_shape):
        if(self.regu_rate < 0):
            self.A = self.add_weight("A", shape=((input_shape[-1]+1) * 2 - 1, self.num_outputs),
                                    initializer="he_uniform", trainable=True)
            self.b = self.add_weight("b", shape=(self.num_outputs,), initializer="he_uniform",trainable=True)
        else:
            self.A = self.add_weight("A", shape=((input_shape[-1]+1) * 2 - 1, self.num_outputs),
                                    initializer="he_uniform", regularizer=tf.keras.regularizers.L2(l2=self.regu_rate), trainable=True)
            self.b = self.add_weight("b", shape=(self.num_outputs,), initializer="he_uniform", 
                                    regularizer=tf.keras.regularizers.L2(l2=self.regu_rate),trainable=True)

    def compute_output_shape(self, input_shape):
        return [input_shape[0], input_shape[1], self.num_outputs]

    @tf.function
    def call(self, x, mask):
        ### a and b contain copies for each pf_Cand:
        x_shape = tf.shape(x)

        ## a tensor: a[n_tau, pf_others, pf, features]
        rep = tf.stack([1,x_shape[1],1])
        a   = tf.tile(x, rep)
        a   = tf.reshape(a,(x_shape[0],x_shape[1],x_shape[1],x_shape[2]))

        ## b tensor: a[n_tau, pf, pf_others, features]
        rep = tf.stack([1,1,x_shape[1]])
        b   = tf.tile(x, rep)
        b   = tf.reshape(b,(x_shape[0],x_shape[1],x_shape[1],x_shape[2]))


        ### Compute distances:
        ca = a[:,:,:, -self.n_dim:]
        cb = b[:,:,:, -self.n_dim:]
        c_shape = tf.shape(ca)
        diff = ca-cb
        diff = tf.math.square(diff)
        dist = tf.math.reduce_sum(diff, axis = -1)
        dist = tf.reshape(dist,(c_shape[0],c_shape[1],c_shape[2],1)) # needed to concat
        na   = tf.concat((a,dist),axis=-1) #a[n_tau, pf_others, pf, features+1]


        ### Weighted sum of features:
        w = tf.math.exp(-10*na[:,:,:,-1]) # weights
        w_shape = tf.shape(w)
        w    = tf.reshape(w,(w_shape[0],w_shape[1],w_shape[2],1)) # needed for multiplication
        mask = tf.reshape(mask, (w_shape[0],w_shape[1],1)) # needed for multiplication
        ## copies of mask:
        rep  = tf.stack([1,w_shape[1],1])
        mask_copy = tf.tile(mask, rep)
        mask_copy = tf.reshape(mask_copy,(w_shape[0],w_shape[1],w_shape[2],1))
        # mask_copy = [n_tau, n_pf_others, n_pf, mask]
        s = na * w * mask_copy # weighted na
        ss = tf.math.reduce_sum(s, axis = 1) # weighted sum of features
        # ss = [n_tau, n_pf, features+1]
        self_dist = tf.zeros((x_shape[0], x_shape[1], 1))
        xx = tf.concat([x, self_dist], axis = 2) # [n_tau, n_pf, features+1]
        ss = ss - xx # difference between weighted features and original ones
        x = tf.concat((x, ss), axis = 2) # add to original features
        # print('check x shape 2: ', x) #(n_tau, n_pf, features*2+1)


        ### Ax+b:
        output = tf.matmul(x, self.A) + self.b

        # print('output.shape: ', output.shape)
        output = output * mask # reapply mask to be sure

        return output

class MyGNN(tf.keras.Model):

    def __init__(self, map_features, **kwargs):
        super(MyGNN, self).__init__()
        self.map_features = map_features
        self.mode = kwargs["mode"]

        self.n_gnn_layers      = kwargs["n_gnn_layers"]
        self.n_dim_gnn         = kwargs["n_dim_gnn"]
        self.n_output_gnn      = kwargs["n_output_gnn"]
        self.n_output_gnn_last = kwargs["n_output_gnn_last"]
        self.n_dense_layers    = kwargs["n_dense_layers"]
        self.n_dense_nodes     = kwargs["n_dense_nodes"]
        self.wiring_mode       = kwargs["wiring_mode"]
        self.dropout_rate      = kwargs["dropout_rate"]
        self.regu_rate         = kwargs["regu_rate"]

        self.embedding1   = tf.keras.layers.Embedding(8,  2)
        self.embedding2   = tf.keras.layers.Embedding(8  ,2)
        self.embedding3   = tf.keras.layers.Embedding(4  ,2)
        # self.normalize    = StdLayer('mean_std.txt', self.map_features, 5, name='std_layer')
        # self.scale        = ScaleLayer('min_max.txt', self.map_features, [-1,1], name='scale_layer')

        self.GNN_layers  = []
        self.batch_norm  = []
        self.acti_gnn    = []
        self.dense            = []
        self.dense_batch_norm = []
        self.dense_acti       = []
        if(self.dropout_rate > 0):
            self.dropout_gnn = []
            self.dropout_dense    = []

        list_outputs = [self.n_output_gnn] * (self.n_gnn_layers-1) + [self.n_output_gnn_last]
        list_n_dim   = [2] + [self.n_dim_gnn] * (self.n_gnn_layers-1)
        self.n_gnn_layers = len(list_outputs)
        self.n_dense_layers = self.n_dense_layers

        for i in range(self.n_gnn_layers):
            self.GNN_layers.append(MyGNNLayer(n_dim=list_n_dim[i], num_outputs=list_outputs[i], regu_rate = self.regu_rate, name='GNN_layer_{}'.format(i)))
            self.batch_norm.append(tf.keras.layers.BatchNormalization(name='batch_normalization_{}'.format(i)))
            self.acti_gnn.append(tf.keras.layers.Activation("tanh", name='acti_gnn_{}'.format(i)))
            if(self.dropout_rate > 0):
                self.dropout_gnn.append(tf.keras.layers.Dropout(self.dropout_rate ,name='dropout_gnn_{}'.format(i)))

        for i in range(self.n_dense_layers-1):
            if(self.regu_rate < 0):
                self.dense.append(tf.keras.layers.Dense(self.n_dense_nodes, kernel_initializer="he_uniform",
                                    bias_initializer="he_uniform", name='dense_{}'.format(i)))
            else:
                self.dense.append(tf.keras.layers.Dense(self.n_dense_nodes, kernel_initializer="he_uniform",
                                bias_initializer="he_uniform", kernel_regularizer=tf.keras.regularizers.L2(l2=self.regu_rate), 
                                bias_regularizer=tf.keras.regularizers.L2(l2=self.regu_rate), name='dense_{}'.format(i)))
            self.dense_batch_norm.append(tf.keras.layers.BatchNormalization(name='dense_batch_normalization_{}'.format(i)))
            self.dense_acti.append(tf.keras.layers.Activation("sigmoid", name='dense_acti{}'.format(i)))
            if(self.dropout_rate > 0):
                self.dropout_dense.append(tf.keras.layers.Dropout(self.dropout_rate ,name='dropout_dense_{}'.format(i)))

        n_last = 4 if self.mode == "p4_dm" else 2
        # self.dense_dm = tf.keras.layers.Dense(6, kernel_initializer="he_uniform",
        #                         bias_initializer="he_uniform", activation="softmax", name='dense_dm')
        # self.dense_p4 = tf.keras.layers.Dense(2, kernel_initializer="he_uniform",
        #                         bias_initializer="he_uniform", name='dense_p4')
        self.dense2 = tf.keras.layers.Dense(n_last, kernel_initializer="he_uniform",
                                bias_initializer="he_uniform", name='dense2')

    @tf.function
    def call(self, xx):
        x_mask = xx[:,:,self.map_features['pfCand_valid']]

        x_em1 = self.embedding1(tf.abs(xx[:,:,self.map_features['pfCand_particleType']]))
        x_em2 = self.embedding2(tf.abs(xx[:,:,self.map_features['pfCand_pvAssociationQuality']]))
        x_em3 = self.embedding3(tf.abs(xx[:,:,self.map_features['pfCand_fromPV']]))
        # x = self.normalize(xx)
        # x = self.scale(x)

        x_part1 = xx[:,:,:self.map_features['pfCand_particleType']]
        x_part2 = xx[:,:,(self.map_features["pfCand_fromPV"]+1):]
        x = tf.concat((x_em1,x_em2,x_em3,x_part1,x_part2),axis = 2)

        # x = xx

        if(self.wiring_mode=="m2"):
            for i in range(self.n_gnn_layers):
                if i > 1:
                    x = tf.concat([x0, x], axis=2)
                x = self.GNN_layers[i](x, mask=x_mask)
                if i == 0:
                    x0 = x
                x = self.batch_norm[i](x)
                x = self.acti_gnn[i](x)
                if(self.dropout_rate > 0):
                    x = self.dropout_gnn[i](x)
        elif(self.wiring_mode=="m1"):
            for i in range(self.n_gnn_layers):
                x = self.GNN_layers[i](x, mask=x_mask)
                x = self.batch_norm[i](x)
                x = self.acti_gnn[i](x)
                if(self.dropout_rate > 0):
                    x = self.dropout_gnn[i](x)
        elif(self.wiring_mode=="m3"):
            for i in range(self.n_gnn_layers):
                if(i%3==0 and i > 0):
                    x = tf.concat([x0, x], axis=2)
                x = self.GNN_layers[i](x, mask=x_mask)
                if(i%3==0):
                    x0 = x
                x = self.batch_norm[i](x)
                x = self.acti_gnn[i](x)
                if(self.dropout_rate > 0):
                    x = self.dropout_gnn[i](x)


        if("p4" in self.mode):
            xx_p4 = xx[:,:,self.map_features['pfCand_px']:self.map_features['pfCand_E']+1]
            xx_p4_shape = tf.shape(xx_p4)
            xx_p4_other = xx[:,:,self.map_features['pfCand_pt']:self.map_features['pfCand_mass']+1]

            x_coor = x[:,:, -self.n_dim_gnn:]
            x_coor = tf.math.square(x_coor)
            d = tf.square(tf.math.reduce_sum(x_coor, axis = -1))
            w = tf.reshape(tf.math.exp(-10*d), (xx_p4_shape[0], xx_p4_shape[1], 1))

            x_mask_shape = tf.shape(x_mask)
            x_mask = tf.reshape(x_mask, (x_mask_shape[0], x_mask_shape[1], 1))
            sum_p4 = tf.reduce_sum(xx_p4 * w * x_mask, axis=1)
            # print('sum_p4.shape: ', sum_p4.shape) #(100,4)
            sum_p4_other = self.ToPtM2(sum_p4)

            x = tf.concat([x, xx_p4, xx_p4_other], axis = 2)

            #xx_p4 = tf.reshape(xx_p4, (xx_p4_shape[0], xx_p4_shape[1] * xx_p4_shape[2]))
            x_shape = tf.shape(x)
            x = tf.reshape(x, (x_shape[0], x_shape[1] * x_shape[2]))
            x = tf.concat([x, sum_p4, sum_p4_other], axis = 1)
            
        elif("dm"==self.mode):
            x_shape = tf.shape(x)
            x = tf.reshape(x, (x_shape[0], x_shape[1] * x_shape[2]))


        for i in range(self.n_dense_layers-1):
            x = self.dense[i](x)
            x = self.dense_batch_norm[i](x)
            x = self.dense_acti[i](x)
            if(self.dropout_rate > 0):
                x = self.dropout_dense[i](x)
        ### dm 6 outputs:
        # x_dm = self.dense_dm(x)
        # x_p4 = self.dense_p4(x)
        # return tf.concat([x_dm, x_p4], axis=1)
        ###
        
        x = self.dense2(x)

        x_zeros = tf.zeros((x_shape[0], 2))
        if(self.mode == "dm"):
            xout = tf.concat([x, x_zeros], axis=1)
        elif self.mode == "p4":
            xout = tf.concat([x_zeros, x], axis=1)
        else:
            xout = x

        # print('xout shape: ',xout)
        return xout

    def ToPtM2(self, x):
        mypx  = x[:,0]
        mypy  = x[:,1]
        mypz  = x[:,2]
        myE   = x[:,3]

        mypx2  = tf.square(mypx)
        mypy2  = tf.square(mypy)
        mypz2  = tf.square(mypz)
        myE2   = tf.square(myE)

        mypt   = tf.sqrt(mypx2 + mypy2)
        mymass = myE2 - mypx2 - mypy2 - mypz2
        absp   = tf.sqrt(mypx2 + mypy2 + mypz2)

        return tf.stack([mypt,mymass], axis=1)

def create_model(setup_main, input_map):
    model = MyGNN(map_features = input_map, **setup_main) # creates the model
    return model

def compile_model(model, mode, learning_rate):
    # opt = tf.keras.optimizers.Nadam(learning_rate=learning_rate, beta_1=1e-4)
    opt = tf.keras.optimizers.Adam(learning_rate = learning_rate)
    CustomMSE.mode = mode
    metrics = []
    if "dm" in mode:
        metrics.extend([my_acc, my_mse_ch, my_mse_neu])
    if "p4" in mode:
        metrics.extend([my_mse_pt, my_mse_mass, pt_res, pt_res_rel, m2_res])
    model.compile(loss=CustomMSE(), optimizer=opt, metrics=metrics)


def run_training(train_suffix, model_name, model, data_loader, is_profile):

    gen_train = dataloader.get_generator(primary_set = True)
    gen_val = dataloader.get_generator(primary_set = False)

    data_train = tf.data.Dataset.from_generator(
        gen_train, output_types = input_types, output_shapes = input_shape
        ).prefetch(tf.data.AUTOTUNE)
    data_val = tf.data.Dataset.from_generator(
        gen_val, output_types = input_types, output_shapes = input_shape
        ).prefetch(tf.data.AUTOTUNE)

    train_name = '%s_%s' % (model_name, train_suffix)
    log_name = "%s.log" % train_name
    if os.path.isfile(log_name):
        close_file(log_name)
        os.remove(log_name)
    csv_log = CSVLogger(log_name, append=True)
    time_checkpoint = TimeCheckpoint(1*60*60, train_name)
    callbacks = [time_checkpoint, csv_log]

    if is_profile:
        logs = "logs/" + model_name + datetime.now().strftime("%Y%m%d-%H%M%S")
        tboard_callback = tf.keras.callbacks.TensorBoard(log_dir = logs, profile_batch='10, 30')
        callbacks.append(tboard_callback)

    fit_hist = model.fit(data_train, validation_data = data_val,
                         epochs = data_loader.n_epochs, initial_epoch = data_loader.epoch,
                         callbacks = callbacks)

    model.save("%s_final.tf" % train_name, save_format="tf")
    return fit_hist


config   = os.path.abspath( "../../configs/trainingReco_v1.yaml")
scaling  = os.path.abspath("../../configs/scaling_params_vReco_v1.json")
dataloader = DataLoader.DataLoader(config, scaling)
input_map, input_shape, input_types  = dataloader.get_config()

setup_main = {
    "mode"             : "p4_dm",
    "n_gnn_layers"     : 5,
    "n_dim_gnn"        : 2,
    "n_output_gnn"     : 50,
    "n_output_gnn_last": 50,
    "n_dense_layers"   : 4,
    "n_dense_nodes"    : 200,
    "wiring_mode"      : "m3",
    "dropout_rate"     : 0.1 ,
    "regu_rate"        : 0.01
}
    
model_name = "RecoSNNv0"
model = create_model(setup_main, input_map)
compile_model(model, setup_main["mode"], 1e-2)
print(input_shape[0])
model.build(input_shape[0])
model.summary()
# tf.keras.utils.plot_model(model, model_name + "_diagram.png", show_shapes=True)
fit_hist = run_training('step{}'.format(1), model_name, model, dataloader, False)