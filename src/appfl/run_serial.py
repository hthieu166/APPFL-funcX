from cmath import nan

from collections import OrderedDict
import torch.nn as nn
from torch.optim import *
from torch.utils.data import DataLoader

import numpy as np

from omegaconf import DictConfig

import copy
import time
import logging

from .misc import *
from .algorithm import *
 
def run_serial(
    cfg: DictConfig,
    model: nn.Module,
    train_data: Dataset,
    test_data: Dataset = Dataset(),
    DataSet_name: str = "appfl",
):
    """Run serial simulation of PPFL.

    Args:
        cfg (DictConfig): the configuration for this run
        model (nn.Module): neural network model to train
        train_data (Dataset): training data
        test_data (Dataset): optional testing data. If given, validation will run based on this data.
        DataSet_name (str): optional dataset name
    """

    ## Logger
    logger = logging.getLogger(__name__)
    logger = create_custom_logger(logger, cfg)

    cfg["logginginfo"]["comm_size"] = 1
    cfg["logginginfo"]["DataSet_name"] = DataSet_name


    """ weight calculation """
    total_num_data = 0
    for k in range(cfg.num_clients):
        total_num_data += len(train_data[k])

    weights = {}
    for k in range(cfg.num_clients):
        weights[k] = len(train_data[k]) / total_num_data

    "Run validation if test data is given or the configuration is enabled."
    if cfg.validation == True and len(test_data) > 0:
        test_dataloader = DataLoader(
            test_data,
            num_workers=cfg.num_workers,
            batch_size=cfg.test_data_batch_size,
            shuffle=cfg.test_data_shuffle,
        )
    else:
        cfg.validation = False


    server = eval(cfg.fed.servername)(
        weights, copy.deepcopy(model), cfg.num_clients, cfg.device, **cfg.fed.args
    )

    server.model.to(cfg.device)

    batchsize = {}
    for k in range(cfg.num_clients):
        batchsize[k] = cfg.train_data_batch_size
        if cfg.batch_training == False:
            batchsize[k] = len(train_data[k])

    clients = [
        eval(cfg.fed.clientname)(
            cfg,
            k,
            weights[k],
            copy.deepcopy(model),
            DataLoader(
                train_data[k],
                num_workers=cfg.num_workers,
                batch_size=batchsize[k],
                shuffle=cfg.train_data_shuffle,
            ), 
            cfg.device,
            **cfg.fed.args,
        )
        for k in range(cfg.num_clients)
    ] 

    ## name of parameters
    model_name=[]
    for name, _ in server.model.named_parameters():
        model_name.append(name)
    
    ## outputs (clients)   
    outfile={}; outdir={}
    for k, client in enumerate(clients): 
        output_filename = cfg.output_filename + "_local_client_%s" %(k)
        outfile[k], outdir[k]=client.write_result_title(output_filename)
 
    start_time = time.time()
    test_loss = 0.0
    accuracy = 0.0
    BestAccuracy = 0.0
    for t in range(cfg.num_epochs):
        PerIter_start = time.time()

        local_states = [OrderedDict()]

        global_state = server.model.state_dict()
        
        LocalUpdate_start = time.time()
        for k, client in enumerate(clients):    
             
            ## initial point for a client model
            for name in server.model.state_dict():
                if name not in model_name:
                    global_state[name] = client.model.state_dict()[name]
            client.model.load_state_dict(global_state)

            ## client update
            local_states[0][k], outfile[k] = client.update(outfile[k], outdir[k], test_dataloader)

  
        cfg["logginginfo"]["LocalUpdate_time"] = time.time() - LocalUpdate_start

        GlobalUpdate_start = time.time()
        server.update(local_states)
        cfg["logginginfo"]["GlobalUpdate_time"] = time.time() - GlobalUpdate_start

        Validation_start = time.time()
        if cfg.validation == True:
            test_loss, accuracy = validation(server, server.model, test_dataloader)
            if accuracy > BestAccuracy:
                BestAccuracy = accuracy

        cfg["logginginfo"]["Validation_time"] = time.time() - Validation_start
        cfg["logginginfo"]["PerIter_time"] = time.time() - PerIter_start
        cfg["logginginfo"]["Elapsed_time"] = time.time() - start_time
        cfg["logginginfo"]["test_loss"] = test_loss
        cfg["logginginfo"]["accuracy"] = accuracy
        cfg["logginginfo"]["BestAccuracy"] = BestAccuracy

        server.logging_iteration(cfg, logger, t)

        """ Saving model """
        if (t + 1) % cfg.checkpoints_interval == 0 or t + 1 == cfg.num_epochs:
            if cfg.save_model == True:
                save_model_iteration(t + 1, server.model, cfg)

    server.logging_summary(cfg, logger)

    for k, client in enumerate(clients):    
        outfile[k].close()