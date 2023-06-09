import os
import random
import torch
import torch.optim as optim
import customAudioDataset as data
from customAudioDataset import collate_fn
from utils import set_seed
from tqdm import tqdm
from model import EncodecModel 
from msstftd import MultiScaleSTFTDiscriminator
from losses import total_loss, disc_loss
from scheduler import WarmupCosineLrScheduler
import torch.distributed as dist
import torch.multiprocessing as mp
import hydra
import logging
import warnings
warnings.filterwarnings("ignore")

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Define train one step function
def train_one_step(epoch,optimizer,optimizer_disc, model, disc_model, trainloader,config,scheduler,disc_scheduler):
    """train one step function

    Args:
        epoch (int): current epoch
        optimizer (_type_) : generator optimizer
        optimizer_disc (_type_): discriminator optimizer
        model (_type_): generator model
        disc_model (_type_): discriminator model
        trainloader (_type_): train dataloader
        config (_type_): hydra config file
        scheduler (_type_): adjust generate model learning rate
        disc_scheduler (_type_): adjust discriminator model learning rate
        warmup_scheduler (_type_): warmup learning rate
    """
    model.train()
    disc_model.train()
    for input_wav in tqdm(trainloader):
        # warmup learning rate, warmup_epoch is defined in config file,default is 5
        input_wav = input_wav.cuda() #[B, 1, T]: eg. [2, 1, 203760]
        optimizer.zero_grad()
        optimizer_disc.zero_grad()
        output, loss_w, _ = model(input_wav) #output: [B, 1, T]: eg. [2, 1, 203760] | loss_w: [1] 
        logits_real, fmap_real = disc_model(input_wav)
        logits_fake, fmap_fake = disc_model(output)
        loss_g = total_loss(fmap_real, logits_fake, fmap_fake, input_wav, output) 
        loss = loss_g + loss_w
        loss.backward()
        optimizer.step()
        scheduler.step()
        # train discriminator when epoch > warmup_epoch and train_discriminator is True
        if config.model.train_discriminator and epoch > config.lr_scheduler.warmup_epoch:
            logits_fake, _ = disc_model(output.detach()) # detach to avoid backpropagation to model
            loss_disc = disc_loss([logit_real.detach() for logit_real in logits_real], logits_fake) # compute discriminator loss
            loss_disc.backward() 
            optimizer_disc.step()
            disc_scheduler.step()
        
    if not config.distributed.data_parallel or dist.get_rank()==0:
        logger.info(f'| epoch: {epoch} | loss: {loss.item()} | loss_g: {loss_g.item()} | loss_w: {loss_w.item()} | lr: {optimizer.param_groups[0]["lr"]} | disc_lr: {optimizer_disc.param_groups[0]["lr"]}')
        if config.model.train_discriminator and epoch > config.lr_scheduler.warmup_epoch:
            logger.info(f'| loss_disc: {loss_disc.item()}')

@torch.no_grad()
def test(epoch,model, disc_model, testloader,config):
    model.eval()
    for input_wav in tqdm(testloader):
        input_wav = input_wav.cuda() #[B, 1, T]: eg. [2, 1, 203760]

        output = model(input_wav) #output: [B, 1, T]: eg. [2, 1, 203760] | loss_w: [1] 
        logits_real, fmap_real = disc_model(input_wav)
        logits_fake, fmap_fake = disc_model(output)
        loss_disc = disc_loss(logits_real, logits_fake) # compute discriminator loss
        
        loss_g = total_loss(fmap_real, logits_fake, fmap_fake, input_wav, output) 

    if not config.distributed.data_parallel or dist.get_rank()==0:
        logger.info(f'| TEST | epoch: {epoch} | loss_g: {loss_g.item()} | loss_disc: {loss_disc.item()}')


def train(local_rank,world_size,config):
    """train main function."""
    # set logger
    file_handler = logging.FileHandler(f"train_encodec_bs{config.datasets.batch_size}_lr{config.optimization.lr}.log")
    formatter = logging.Formatter('%(asctime)s: %(levelname)s: [%(filename)s: %(lineno)d]: %(message)s')
    file_handler.setFormatter(formatter)

    # print to screen
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.ERROR)

    # add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # set seed
    if config.common.seed is not None:
        set_seed(config.common.seed)
    
    # set train dataset
    trainset = data.CustomAudioDataset(config=config)
    testset = data.CustomAudioDataset(config=config,mode='test')
    # set encodec model and discriminator model
    model = EncodecModel._get_model(
                config.model.target_bandwidths, 
                config.model.sample_rate, 
                config.model.channels,
                causal=False, model_norm='time_group_norm', 
                audio_normalize=config.model.audio_normalize,
                segment=1., name='my_encodec')
    disc_model = MultiScaleSTFTDiscriminator(filters=config.model.filters)

    # log model, disc model parameters and train mode
    logger.info(config)
    logger.info(f"Encodec Model Parameters: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    logger.info(f"Disc Model Parameters: {sum(p.numel() for p in disc_model.parameters() if p.requires_grad)}")
    logger.info(f"model train mode :{model.training} | quantizer train mode :{model.quantizer.training} ")

    # resume training
    resume_epoch = 1
    if config.checkpoint.resume:
        # check the checkpoint_path
        assert config.checkpoint.checkpoint_path != '', "resume path is empty"
        assert config.checkpoint.disc_checkpoint_path != '', "disc resume path is empty"

        model_checkpoint = torch.load(config.checkpoint.checkpoint_path, map_location='cpu')
        disc_model_checkpoint = torch.load(config.checkpoint.disc_checkpoint_path, map_location='cpu')
        model.load_state_dict(model_checkpoint['model_state_dict'])
        disc_model.load_state_dict(disc_model_checkpoint['model_state_dict'])
        resume_epoch = model_checkpoint['epoch']
        if resume_epoch > config.common.max_epoch:
            raise ValueError(f"resume epoch {resume_epoch} is larger than total epochs {config.common.epochs}")

    train_sampler = None
    test_sampler = None
    if config.distributed.data_parallel:
        # distributed init
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '12455'
        torch.distributed.init_process_group(backend='nccl',rank=local_rank,world_size=world_size)
        torch.cuda.set_device(local_rank) 
        torch.cuda.empty_cache()
        # set distributed sampler
        train_sampler = torch.utils.data.distributed.DistributedSampler(trainset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(testset)
    
    model.cuda()
    disc_model.cuda()

    trainloader = torch.utils.data.DataLoader(
        trainset,
        batch_size=config.datasets.batch_size,
        sampler=train_sampler, 
        shuffle=(train_sampler is None), collate_fn=collate_fn,
        pin_memory=config.datasets.pin_memory)
    testloader = torch.utils.data.DataLoader(
        testset,
        batch_size=config.datasets.batch_size,
        sampler=test_sampler, 
        shuffle=False, collate_fn=collate_fn,
        pin_memory=config.datasets.pin_memory)


    # set optimizer and scheduler, warmup scheduler
    params = [p for p in model.parameters() if p.requires_grad]
    disc_params = [p for p in disc_model.parameters() if p.requires_grad]
    optimizer = optim.Adam([{'params': params, 'lr': config.optimization.lr}], betas=(0.5, 0.9))
    optimizer_disc = optim.Adam([{'params':disc_params, 'lr': config.optimization.disc_lr}], betas=(0.5, 0.9))
    scheduler = WarmupCosineLrScheduler(optimizer, max_iter=config.common.max_epoch*len(trainloader), eta_ratio=0.1, warmup_iter=config.lr_scheduler.warmup_epoch*len(trainloader), warmup_ratio=1e-4)
    disc_scheduler = WarmupCosineLrScheduler(optimizer_disc, max_iter=config.common.max_epoch*len(trainloader), eta_ratio=0.1, warmup_iter=config.lr_scheduler.warmup_epoch*len(trainloader), warmup_ratio=1e-4)
    # scheduler = CosineAnnealingLR(optimizer, T_max=100, eta_min=0)
    # disc_scheduler = CosineAnnealingLR(optimizer_disc, T_max=100, eta_min=0)
    # iter_per_epoch = len(trainloader)
    # warmup_scheduler = WarmUpLR(optimizer, iter_per_epoch,config.lr_scheduler.warmup_epoch)
    if config.checkpoint.resume and 'scheduler_state_dict' in model_checkpoint.keys() and 'scheduler_state_dict' in disc_model_checkpoint.keys(): 
        optimizer.load_state_dict(model_checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(model_checkpoint['scheduler_state_dict'])
        optimizer_disc.load_state_dict(disc_model_checkpoint['optimizer_state_dict'])
        disc_scheduler.load_state_dict(disc_model_checkpoint['scheduler_state_dict'])

    if config.distributed.data_parallel:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        disc_model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(disc_model)
        # wrap the model by using DDP
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[local_rank],
            output_device=local_rank,
            broadcast_buffers=False,
            find_unused_parameters=config.distributed.find_unused_parameters)
        disc_model = torch.nn.parallel.DistributedDataParallel(
            disc_model,
            device_ids=[local_rank],
            output_device=local_rank,
            broadcast_buffers=False,
            find_unused_parameters=config.distributed.find_unused_parameters)
    
    start_epoch = max(1,resume_epoch) # start epoch is 1 if not resume
    for epoch in range(start_epoch, config.common.max_epoch+1):
        train_one_step(
            epoch, optimizer, optimizer_disc, 
            model, disc_model, trainloader,config,
            scheduler,disc_scheduler)
        if epoch % config.common.test_interval == 0:
            test(epoch,model,disc_model,testloader,config)
        # save checkpoint and epoch
        if epoch % config.common.save_interval == 0:
            if config.distributed.data_parallel and dist.get_rank()==0:
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': model.module.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'scheduler_state_dict': scheduler.state_dict(),
                }, f'{config.checkpoint.save_location}epoch{epoch}_lr{config.optimization.lr}.pt')
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': disc_model.module.state_dict(),
                    'optimizer_state_dict': optimizer_disc.state_dict(),
                    'scheduler_state_dict': disc_scheduler.state_dict(),
                },f'{config.checkpoint.save_location}epoch{epoch}_disc_lr{config.optimization.lr}.pt')
            elif not config.distributed.data_parallel:
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'scheduler_state_dict': scheduler.state_dict(),
                }, f'{config.checkpoint.save_location}epoch{epoch}_lr{config.optimization.lr}.pt')
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': disc_model.state_dict(),
                    'optimizer_state_dict': optimizer_disc.state_dict(),
                    'scheduler_state_dict': disc_scheduler.state_dict(),
                },f'{config.checkpoint.save_location}epoch{epoch}_disc_lr{config.optimization.lr}.pt')
    if config.distributed.data_parallel:
        dist.destroy_process_group()

@hydra.main(config_path='config', config_name='config')
def main(config):
    if config.distributed.torch_distributed_debug: # set distributed debug, if you encouter some multi gpu bug, please set torch_distributed_debug=True
        os.environ["TORCH_CPP_LOG_LEVEL"]="INFO"
        os.environ["TORCH_DISTRIBUTED_DEBUG"]="DETAIL"
    if not os.path.exists(config.checkpoint.save_folder):
        os.makedirs(config.checkpoint.save_folder)
    # set distributed
    if config.distributed.data_parallel:
        world_size=config.distributed.world_size
        torch.multiprocessing.set_start_method('spawn')
        mp.spawn(
            train,
            args=(world_size,config,),
            nprocs=world_size,
            join=True
        )
    else:
        train(1,1,config) # set single gpu train


if __name__ == '__main__':
    main()
