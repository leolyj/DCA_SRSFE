import os, sys
import os.path as osp
import time
from functools import partial
import gc
import traceback

import numpy as np

import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data

import transforms
import datasets
import models
import cmd_args
from main_utils import *
from tqdm import tqdm

from models import EPE3DLoss
from evaluation_bnn import evaluate

from models import ChamferLoss, RigidLoss, RigidLossVis, DistanceLoss, LaplaceLoss, LaplaceLoss_0
torch.distributed.init_process_group(backend="nccl")  # distributed .........................
torch.autograd.set_detect_anomaly(True)


class WeightEMA (object):
    """
    Exponential moving average weight optimizer for mean teacher model
    """
    def __init__(self, params, src_params, alpha=0.999):
        self.params = list(params)
        self.src_params = list(src_params)
        self.alpha = alpha

        for p, src_p in zip(self.params, self.src_params):
            p.data[:] = src_p.data[:]
        print('teacher model initialized ...')

    def step(self):
        one_minus_alpha = 1.0 - self.alpha
        #li = 0
        for p, src_p in zip(self.params, self.src_params):
            p.data.mul_(self.alpha)
            p.data.add_(src_p.data * one_minus_alpha)
            #  check if teacher's weights are updated
            #if li == 10 or li == 11:
            #    print('student----- ', src_p.data)
            #    print(' ')
            #    print('teacher----- ', p.data)
            #li += 1


def main():
    # ensure numba JIT is on
    if 'NUMBA_DISABLE_JIT' in os.environ:
        del os.environ['NUMBA_DISABLE_JIT']

    # parse arguments
    global args
    #args = cmd_args.parse_args_from_yaml(sys.argv[-1])
    # args = cmd_args.parse_args_from_yaml('configs/train_ours_waymo.yaml')
    args = cmd_args.parse_args_from_yaml('configs/test_ours_waymo.yaml')

    # -------------------- logging args --------------------
    '''
    if osp.exists(args.ckpt_dir):
        to_continue = query_yes_no('Attention!!!, ckpt_dir already exists!\
                                        Whether to continue?',
                                   default=None)
        if not to_continue:
            sys.exit(1)
    '''
    os.makedirs(args.ckpt_dir, mode=0o777, exist_ok=True)
    
    vis_logger = Logger(osp.join('/media/asus/bff416fa-b0ca-4c6a-b793-de319cc5ad0b/jz/HPLFlowNet/checkpoints/train/waymo_data', 'log'))


    logger = Logger(osp.join(args.ckpt_dir, 'log'))
    logger.log('sys.argv:\n' + ' '.join(sys.argv))
    
    #os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda)  # gpu
    local_rank = torch.distributed.get_rank()
    print('local rank ', local_rank)
    torch.cuda.set_device(local_rank)

    os.environ['NUMBA_NUM_THREADS'] = str(args.workers)
    logger.log('NUMBA NUM THREADS\t' + os.environ['NUMBA_NUM_THREADS'])

    for arg in sorted(vars(args)):
        logger.log('{:20s} {}'.format(arg, getattr(args, arg)))
    logger.log('')

    # -------------------- dataset & loader --------------------
    if not args.evaluate:
        # source dataset
        train_dataset_source = datasets.__dict__[args.dataset](
            train=True,
            transform=transforms.Augmentation(args.aug_together,
                                              args.aug_pc2,
                                              args.data_process,
                                              args.num_points,
                                              args.allow_less_points),
            gen_func=transforms.GenerateDataUnsymmetric(args),
            args=args
        )
        logger.log('train_dataset_source: ' + str(train_dataset_source))
        '''
        train_loader = torch.utils.data.DataLoader(
            train_dataset_source,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=args.workers,
            pin_memory=True,
            worker_init_fn=lambda x: np.random.seed((torch.initial_seed()) % (2 ** 32))
        )'''
        
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset_source)

        train_loader = torch.utils.data.DataLoader(
		train_dataset_source, 
		batch_size=args.batch_size, 
		shuffle=False, 
		num_workers=args.workers, 
		pin_memory=True, 
		drop_last=True,
		sampler=train_sampler)
        

    # -----------------------val_dataset ----- target_dataset-----------------------------------
    val_dataset = datasets.__dict__[args.val_dataset](
        train=False,
        transform=transforms.ProcessData(args.data_process,
                                         args.num_points,
                                         args.allow_less_points),
        gen_func=transforms.GenerateDataUnsymmetric(args),
        args=args
    )
    logger.log('val_dataset: ' + str(val_dataset))
    '''
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        worker_init_fn=lambda x: np.random.seed((torch.initial_seed()) % (2 ** 32))
    )'''
    val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)

    val_loader = torch.utils.data.DataLoader(
	    val_dataset, 
	    batch_size=args.batch_size, 
	    shuffle=False, 
	    num_workers=args.workers, 
	    pin_memory=True, 
	    drop_last=True,
	    sampler=val_sampler)

    # -------------------- create model --------------------
    #logger.log("=>  creating model '{}'".format(args.arch))
    # student model
    student_model = models.__dict__[args.arch](args)

    if not args.evaluate:
        init_func = partial(init_weights_multi, init_type=args.init, gain=args.gain)
        student_model.apply(init_func)
    #logger.log(student_model)
    
    student_model = student_model.cuda()
    device = torch.device('cuda:%d' % local_rank)  
    student_model = student_model.to(device)
    student_model=torch.nn.parallel.DistributedDataParallel(student_model, device_ids=[local_rank])
    
    '''
    if torch.cuda.device_count() > 1:
        print('device_count ', torch.cuda.device_count())
        cuda_device = list(range(torch.cuda.device_count()))
        #student_model = torch.nn.DataParallel(student_model, device_ids=cuda_device)
        student_model=torch.nn.parallel.DistributedDataParallel(student_model)
    else:
        student_model = torch.nn.DataParallel(student_model).cuda()
    '''
    criterion_EPE3D = EPE3DLoss().cuda().to(device)

    
    '''
    if torch.cuda.device_count() > 1:
        #teacher_model = torch.nn.DataParallel(teacher_model, device_ids=cuda_device)
        teacher_model=torch.nn.parallel.DistributedDataParallel(teacher_model, device_ids=[local_rank])
    else:
        teacher_model = torch.nn.DataParallel(teacher_model).cuda()
    '''

    if args.evaluate:
        torch.backends.cudnn.enabled = False
    else:
        cudnn.benchmark = True
    # https://discuss.pytorch.org/t/what-does-torch-backends-cudnn-benchmark-do/5936
    # But if your input sizes changes at each iteration,
    # then cudnn will benchmark every time a new size appears,
    # possibly leading to worse runtime performances.

    # -------------------- resume --------------------
    if args.resume:
        if osp.isfile(args.resume):
            logger.log("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            student_model.load_state_dict(checkpoint['state_dict'], strict=True)
            logger.log("=> loaded checkpoint '{}' (start epoch {}, min loss {})"
                       .format(args.resume, checkpoint['epoch'], checkpoint['min_loss']))
        else:
            logger.log("=> no checkpoint found at '{}'".format(args.resume))
            checkpoint = None
    else:
        args.start_epoch = 0

    # -------------------- evaluation --------------------
    if args.evaluate:
        res_str = evaluate(val_loader, student_model, logger, args)
        logger.close()
        return res_str

    # -------------------- optimizer --------------------
    # for student model
    student_model_params = []
    for key, value in dict(student_model.named_parameters()).items():
        if value.requires_grad:
            student_model_params += [value]

    optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, student_model.parameters()),
                                 lr=args.lr,
                                 weight_decay=0)
    if args.resume and (checkpoint is not None):
        optimizer.load_state_dict(checkpoint['optimizer'])


    if hasattr(args, 'reset_lr') and args.reset_lr:
        print('reset lr')
        reset_learning_rate(optimizer, args)

    # -------------------- main loop --------------------
    min_train_loss = None
    best_train_epoch = None
    best_val_epoch = None
    do_eval = True

    for epoch in range(args.start_epoch, args.epochs):
        old_lr = optimizer.param_groups[0]['lr']
        adjust_learning_rate(optimizer, epoch, args)

        lr = optimizer.param_groups[0]['lr']
        if old_lr != lr:
            print('Switch lr!')
        logger.log('lr: ' + str(optimizer.param_groups[0]['lr']))
        
        #target_loader.sampler.set_epoch(epoch)  # shuffle the target dataloader!!!!!!!!!!!!!

        train_loss = train(train_loader, student_model, criterion_EPE3D, optimizer, epoch, logger, device)

        gc.collect()

        is_train_best = True if best_train_epoch is None else (train_loss < min_train_loss)
        if is_train_best:
            min_train_loss = train_loss
            best_train_epoch = epoch

        if do_eval:
            logger.log('--------eval for student---------')
            val_loss = validate(val_loader, student_model, criterion_EPE3D, logger)
            gc.collect()

            is_val_best = True if best_val_epoch is None else (val_loss < min_val_loss)
            if is_val_best:
                min_val_loss = val_loss
                best_val_epoch = epoch
                logger.log("New min val loss!")

        min_loss = min_val_loss if do_eval else min_train_loss
        is_best = is_val_best if do_eval else is_train_best
        # for student
        save_checkpoint({
            'epoch': epoch + 1,  # next start epoch
            'arch': args.arch,
            'state_dict': student_model.state_dict(),
            'min_loss': min_loss,
            'optimizer': optimizer.state_dict(),
        }, is_best, args.ckpt_dir)


    train_str = 'Best train loss: {:.5f} at epoch {:3d}'.format(min_train_loss, best_train_epoch)
    logger.log(train_str)

    if do_eval:
        val_str = 'Best val loss:a {:.5f} at epoch {:3d}'.format(min_val_loss, best_val_epoch)
        logger.log(val_str)

    logger.close()
    result_str = val_str if do_eval else train_str
    return result_str


def split_and_load(batch):
    new_batch = []
    for i, data in enumerate(batch):
        new_data = [x for x in data]
        new_batch.append(new_data)
    return new_batch


def train(train_loader, student_model, criterion_epe, optimizer, epoch, logger, device):
    epe3d_losses = AverageMeter()
    total_losses = AverageMeter()
    pcons_losses = AverageMeter()
    rcons_losses = AverageMeter()
    
    student_model.train()

    for i, (pc1, pc2, sf, generated_data, path) in tqdm(enumerate(train_loader), total=len(train_loader)):

        try:
            cur_sf = sf.cuda(non_blocking=True).to(device)
            output = student_model(pc1, pc2, generated_data)  # student(source)
            
            epe3d_loss = criterion_epe(input=output, target=cur_sf).mean()

            loss = epe3d_loss
            
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epe3d_losses.update(epe3d_loss.item(), pc1.size(0))  # batch size can only be 1 for now
            
            #if epe3d_loss.item() > 4:
            #    print(path[0])

            if i % args.print_freq == 0:
                logger.log('Epoch: [{0}][{1}/{2}]\t'
                           'EPE3D Loss {epe3d_losses_.val:.4f} ({epe3d_losses_.avg:.4f}))'
                            .format(
                            epoch + 1, i + 1, len(train_loader),
                            epe3d_losses_=epe3d_losses), end='')
                #logger.log('point_consistency, rigid_consistency {p_cons:.4f} {r_cons:.4f}'.format(p_cons=point_consistency, r_cons=rigid_consistency))
                logger.log('')

        except RuntimeError as ex:
            print('local rank: ', torch.distributed.get_rank())
            logger.log("in TRAIN, RuntimeError " + repr(ex))
            traceback.print_tb(ex.__traceback__, file=logger.out_fd)
            traceback.print_tb(ex.__traceback__)

            if "CUDA error: out of memory" in str(ex) or "cuda runtime error" in str(ex) or "CUDA out of memory" in str(ex) or "CUDA error" in str(ex):
                logger.log("batch idx: " + str(i) + ' path: ' + path[0])
                logger.log("out of memory, continue")

                del pc1, pc2, sf, generated_data
                if 'output' in locals():
                    print('del output')
                    del output

                torch.cuda.empty_cache()
                print('empty_cache')
                gc.collect()
                print('gc.collect()')
                
                loss = torch.tensor(0.0)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
            else:
                torch.cuda.empty_cache()
                gc.collect()
                print('local rank: ', torch.distributed.get_rank())
                #print('pass')
                #pass  ###########################
                sys.exit(1)

    logger.log(
        ' * Train EPE3D {epe3d_losses_.avg:.4f}'.format(epe3d_losses_=epe3d_losses))
    return epe3d_losses.avg


def validate(val_loader, model, criterion, logger):
    epe3d_losses = AverageMeter()

    model.eval()

    with torch.no_grad():
        for i, (pc1, pc2, sf, generated_data, path) in enumerate(val_loader):
            try:
                cur_sf = sf.cuda(non_blocking=True)
                output = model(pc1, pc2, generated_data)
                epe3d_loss = criterion(input=output, target=cur_sf)

                epe3d_losses.update(epe3d_loss.mean().item())

                if i % args.print_freq == 0:
                    logger.log('Test: [{0}/{1}]\t'
                               'EPE3D loss {epe3d_losses_.val:.4f} ({epe3d_losses_.avg:.4f})'
                               .format(i + 1, len(val_loader),
                                       epe3d_losses_=epe3d_losses))

            except RuntimeError as ex:
                logger.log("in VAL, RuntimeError " + repr(ex))
                traceback.print_tb(ex.__traceback__, file=logger.out_fd)
                traceback.print_tb(ex.__traceback__)

                if "CUDA error: out of memory" in str(ex) or "cuda runtime error" in str(ex):
                    logger.log("out of memory, continue")
                    del pc1, pc2, sf, generated_data
                    torch.cuda.empty_cache()
                    gc.collect()
                    print('remained objects after OOM crash')
                else:
                    sys.exit(1)

    logger.log(' * EPE3D loss {epe3d_loss_.avg:.4f}'.format(epe3d_loss_=epe3d_losses))
    return epe3d_losses.avg


if __name__ == '__main__':
    pid = os.getpid()
    print('pid: ', pid)
    #os.environ['CUDA_VISIBLE_DEVICES'] = '0, 1, 2, 3, 4, 5, 6, 7'
    main()
