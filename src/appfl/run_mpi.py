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

from mpi4py import MPI

 
def run_server(
    cfg: DictConfig,
    comm: MPI.Comm,
    model: nn.Module,
    num_clients: int,
    test_dataset: Dataset = Dataset(),
    DataSet_name: str = "appfl",
):
    """Run PPFL simulation server that aggregates and updates the global parameters of model

    Args:
        cfg (DictConfig): the configuration for this run
        comm: MPI communicator
        model (nn.Module): neural network model to train
        num_clients (int): the number of clients used in PPFL simulation
        test_data (Dataset): optional testing data. If given, validation will run based on this data.
        DataSet_name (str): optional dataset name
    """

    ## Start
    comm_size = comm.Get_size()
    comm_rank = comm.Get_rank()
    num_client_groups = np.array_split(range(num_clients), comm_size - 1)

    # FIXME: I think it's ok for server to use cpu only.
    device = "cpu"

    ## Logger
    logger = logging.getLogger(__name__)
    logger = create_custom_logger(logger, cfg)

    cfg["logginginfo"]["comm_size"] = comm_size
    cfg["logginginfo"]["DataSet_name"] = DataSet_name

    "Run validation if test data is given or the configuration is enabled."
    if cfg.validation == True and len(test_dataset) > 0:
        test_dataloader = DataLoader(
            test_dataset,
            num_workers=cfg.num_workers,
            batch_size=cfg.test_data_batch_size,
            shuffle=cfg.test_data_shuffle,
        )
    else:
        cfg.validation = False

    """
    Receive the number of data from clients
    Compute "weight[client] = data[client]/total_num_data" from a server    
    Scatter "weight information" to clients        
    """
    Num_Data = comm.gather(0, root=0)
    total_num_data = 0
    for rank in range(1, comm_size):
        for val in Num_Data[rank].values():
            total_num_data += val

    weight = []
    weights = {}
    for rank in range(comm_size):
        if rank == 0:
            weight.append(0)
        else:
            temp = {}
            for key in Num_Data[rank].keys():
                temp[key] = Num_Data[rank][key] / total_num_data
                weights[key] = temp[key]
            weight.append(temp)

    weight = comm.scatter(weight, root=0)

    # TODO: do we want to use root as a client?
    server = eval(cfg.fed.servername)(
        weights, copy.deepcopy(model), num_clients, device, **cfg.fed.args
    )

    do_continue = True
    start_time = time.time()
    test_loss = 0.0
    accuracy = 0.0
    BestAccuracy = 0.0
    for t in range(cfg.num_epochs):
        PerIter_start = time.time()
        do_continue = comm.bcast(do_continue, root=0)

        # We need to load the model on cpu, before communicating.
        # Otherwise, out-of-memeory error from GPU
        server.model.to("cpu")

        global_state = server.model.state_dict()

        LocalUpdate_start = time.time()
        global_state = comm.bcast(global_state, root=0)
        local_states = comm.gather(None, root=0)
        cfg["logginginfo"]["LocalUpdate_time"] = time.time() - LocalUpdate_start

        GlobalUpdate_start = time.time()
        server.update(local_states)
        cfg["logginginfo"]["GlobalUpdate_time"] = time.time() - GlobalUpdate_start

        Validation_start = time.time()
        test_loss = 0
        accuracy = 0
        BestAccuracy = 0
        if cfg.validation == True:
            test_loss, accuracy = validation(server, copy.deepcopy(server.model), test_dataloader)
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

        if np.isnan(test_loss) == True:
            break

    """ Summary """
    server.logging_summary(cfg, logger)

    do_continue = False
    do_continue = comm.bcast(do_continue, root=0)


def run_client(
    cfg: DictConfig,
    comm: MPI.Comm,
    model: nn.Module,
    num_clients: int,
    train_data: Dataset,
    test_dataset: Dataset = Dataset(),
):
    """Run PPFL simulation clients, each of which updates its own local parameters of model

    Args:
        cfg (DictConfig): the configuration for this run
        comm: MPI communicator
        model (nn.Module): neural network model to train
        num_clients (int): the number of clients used in PPFL simulation
        train_data (Dataset): training data
        test_data (Dataset): testing data
    """

    comm_size = comm.Get_size()
    comm_rank = comm.Get_rank()

    ## We assume to have as many GPUs as the number of MPI processes.
    if cfg.device == "cuda":
        device = f"cuda:{comm_rank-1}"
    else:
        device = cfg.device

    num_client_groups = np.array_split(range(num_clients), comm_size - 1)

    """
    Send the number of data to a server
    Receive "weight_info" from a server    
        (fedavg)            "weight_info" is not needed as of now.
        (iceadmm+iiadmm)    "weight_info" is needed for constructing coefficients of the loss_function         
    """
    num_data = {}
    for i, cid in enumerate(num_client_groups[comm_rank - 1]):
        num_data[cid] = len(train_data[cid])
    comm.gather(num_data, root=0)
    weight = None
    weight = comm.scatter(weight, root=0)

    batchsize = {}
    for _, cid in enumerate(num_client_groups[comm_rank - 1]):
        batchsize[cid] = cfg.train_data_batch_size
        if cfg.batch_training == False:
            batchsize[cid] = len(train_data[cid])

    clients = [
        eval(cfg.fed.clientname)(
            cfg,
            cid,
            weight[cid],
            copy.deepcopy(model),
            DataLoader(
                train_data[cid],
                num_workers=cfg.num_workers,
                batch_size=batchsize[cid],
                shuffle=cfg.train_data_shuffle,
            ),
            device,
            **cfg.fed.args,
        )
        for i, cid in enumerate(num_client_groups[comm_rank - 1])
    ]
    ##
    if test_dataset != None:
        test_dataloader = DataLoader(
            test_dataset,
            num_workers=cfg.num_workers,
            batch_size=cfg.test_data_batch_size,
            shuffle=cfg.test_data_shuffle,
        )

    ## name of parameters 
    model_name=[]
    for client in clients:
        for name, _ in client.model.named_parameters():
            model_name.append(name)
        break
    
    ## outputs (clients)   
    outfile={}; outdir={}
    for _, cid in enumerate(num_client_groups[comm_rank - 1]):
        output_filename = cfg.output_filename + "_local_client_%s" %(cid)
        outfile[cid], outdir[cid]=client.write_result_title(output_filename)

    do_continue = comm.bcast(None, root=0)

    local_states = OrderedDict()

    while do_continue:
        """Receive "global_state" """
        global_state = comm.bcast(None, root=0)

        """ Update "local_states" based on "global_state" """
        for client in clients:
            cid = client.id
            ## initial point for a client model
            for name in client.model.state_dict():
                if name not in model_name:
                    global_state[name] = client.model.state_dict()[name]
            client.model.load_state_dict(global_state)
             
            ## client update
            local_states[cid], outfile[cid] = client.update(outfile[cid], outdir[cid], test_dataloader)

        """ Send "local_states" to a server """
        comm.gather(local_states, root=0)

        do_continue = comm.bcast(None, root=0)

    for _, cid in enumerate(num_client_groups[comm_rank - 1]):
        outfile[cid].close()