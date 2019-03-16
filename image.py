import sys
import os
import time
import importlib
import argparse

import numpy as np

import torch
import torch.utils.data
from torchvision.utils import save_image
from torch import nn, optim

from modules import ResNetEncoderV2, PixelCNNDecoderV2
from modules import VAE

clip_grad = 5.0
decay_epoch = 20
lr_decay = 0.5
max_decay = 5

def init_config():
    parser = argparse.ArgumentParser(description='VAE mode collapse study')

    # model hyperparameters
    parser.add_argument('--dataset', type=str, required=True, help='dataset to use')

    # optimization parameters
    parser.add_argument('--nsamples', type=int, default=1, help='number of samples for training')
    parser.add_argument('--iw_nsamples', type=int, default=500,
                         help='number of samples to compute importance weighted estimate')

    # select mode
    parser.add_argument('--eval', action='store_true', default=False, help='compute iw nll')
    parser.add_argument('--load_path', type=str, default='')

    # annealing paramters
    parser.add_argument('--warm_up', type=int, default=10)
    parser.add_argument('--kl_start', type=float, default=1.0)

    # inference parameters
    parser.add_argument('--aggressive', type=int, default=0,
                         help='apply aggressive training when nonzero, reduce to vanilla VAE when aggressive is 0')

    # others
    parser.add_argument('--seed', type=int, default=783435, metavar='S', help='random seed')
    parser.add_argument('--sample_from', type=str, default='', help='load model and perform sampling')

    # these are for slurm purpose to save model
    parser.add_argument('--jobid', type=int, default=0, help='slurm job id')
    parser.add_argument('--taskid', type=int, default=0, help='slurm task id')


    args = parser.parse_args()
    args.cuda = torch.cuda.is_available()

    save_dir = "models/%s" % args.dataset

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    seed_set = [783435, 101, 202, 303, 404, 505, 606, 707, 808, 909]
    args.seed = seed_set[args.taskid]

    id_ = "%s_aggressive%d_ns%d_kls%.1f_warm%d_%d_%d_%d" % \
            (args.dataset, args.aggressive, args.nsamples,
             args.kl_start, args.warm_up, args.jobid, args.taskid, args.seed)

    save_path = os.path.join(save_dir, id_ + '.pt')

    args.save_path = save_path

    # load config file into args
    config_file = "config.config_%s" % args.dataset
    params = importlib.import_module(config_file).params

    args = argparse.Namespace(**vars(args), **params)

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)
        torch.backends.cudnn.deterministic = True

    return args


def test(model, test_loader, mode, args):

    report_kl_loss = report_rec_loss = 0
    report_num_examples = 0
    mutual_info = []
    for datum in test_loader:
        batch_data, _ = datum
        batch_size = batch_data.size(0)

        report_num_examples += batch_size


        loss, loss_rc, loss_kl, loss_mul2 = model.loss(batch_data, 1.0, nsamples=args.nsamples)

        assert(not loss_rc.requires_grad)

        loss_rc = loss_rc.sum()
        loss_kl = loss_kl.sum()


        report_rec_loss += loss_rc.item()
        report_kl_loss += loss_kl.item()

    mutual_info = calc_mi(model, test_loader)

    test_loss = (report_rec_loss  + report_kl_loss) / report_num_examples

    nll = (report_kl_loss + report_rec_loss) / report_num_examples
    kl = report_kl_loss / report_num_examples

    print('%s --- avg_loss: %.4f, kl: %.4f, mi: %.4f, recon: %.4f, nll: %.4f' % \
           (mode, test_loss, report_kl_loss / report_num_examples, mutual_info,
            report_rec_loss / report_num_examples, nll))
    sys.stdout.flush()

    return test_loss, nll, kl

def calc_mi(model, test_loader):
    mi = 0
    num_examples = 0
    for datum in test_loader:
        batch_data, _ = datum
        batch_size = batch_data.size(0)
        num_examples += batch_size
        mutual_info = model.calc_mi_q(batch_data)
        mi += mutual_info * batch_size

    return mi / num_examples

def calc_au(model, test_loader, delta=0.01):
    """compute the number of active units
    """
    means = []
    for datum in test_loader:
        batch_data, _ = datum
        mean, _ = model.encode_stats(batch_data)
        means.append(mean)

    means = torch.cat(means, dim=0)
    au_mean = means.mean(0, keepdim=True)

    # (batch_size, nz)
    au_var = means - au_mean
    ns = au_var.size(0)

    au_var = (au_var ** 2).sum(dim=0) / (ns - 1)

    return (au_var >= delta).sum().item(), au_var

def calc_iwnll(model, test_loader, args):

    report_nll_loss = 0
    report_num_examples = 0
    for id_, datum in enumerate(test_loader):
        batch_data, _ = datum
        batch_size = batch_data.size(0)

        report_num_examples += batch_size

        if id_ % (round(len(test_loader) / 10)) == 0:
            print('iw nll computing %d0%%' % (id_/(round(len(test_loader) / 10))))
            sys.stdout.flush()

        loss = model.nll_iw(batch_data, nsamples=args.iw_nsamples)

        report_nll_loss += loss.sum().item()

    nll = report_nll_loss / report_num_examples

    print('iw nll: %.4f' % nll)
    sys.stdout.flush()
    return nll

def main(args):
    if args.save_path == '':
        make_savepath(args)
        seed(args)

    if args.cuda:
        print('using cuda')

    print(args)

    device = torch.device("cuda" if args.cuda else "cpu")
    args.device = device

    opt_dict = {"not_improved": 0, "lr": 1., "best_loss": 1e4}

    all_data = torch.load(args.data_file)
    x_train, x_val, x_test = all_data

    x_train = x_train.to(device)
    x_val = x_val.to(device)
    x_test = x_test.to(device)
    y_size = 1
    y_train = x_train.new_zeros(x_train.size(0), y_size)
    y_val = x_train.new_zeros(x_val.size(0), y_size)
    y_test = x_train.new_zeros(x_test.size(0), y_size)
    print(torch.__version__)
    train_data = torch.utils.data.TensorDataset(x_train, y_train)
    val_data = torch.utils.data.TensorDataset(x_val, y_val)
    test_data = torch.utils.data.TensorDataset(x_test, y_test)

    train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=True)
    val_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batch_size, shuffle=True)
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=args.batch_size, shuffle=True)
    print('Train data: %d batches' % len(train_loader))
    print('Val data: %d batches' % len(val_loader))
    print('Test data: %d batches' % len(test_loader))
    sys.stdout.flush()

    log_niter = len(train_loader)//5

    encoder = ResNetEncoderV2(args)
    decoder = PixelCNNDecoderV2(args)

    vae = VAE(encoder, decoder, args).to(device)

    if args.sample_from != '':
        save_dir = "samples/%s" % args.dataset
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        vae.load_state_dict(torch.load(args.sample_from))
        vae.eval()
        with torch.no_grad():
            sample_z = vae.sample_from_prior(400).to(device)
            sample_x, sample_probs = vae.decode(sample_z, False)
        image_file = 'sample_binary_from_%s.png' % (args.sample_from.split('/')[-1][:-3])
        save_image(sample_x.data.cpu(), os.path.join(save_dir, image_file), nrow=20)
        image_file = 'sample_cont_from_%s.png' % (args.sample_from.split('/')[-1][:-3])
        save_image(sample_probs.data.cpu(), os.path.join(save_dir, image_file), nrow=20)

        return

    if args.eval:
        print('begin evaluation')
        test_loader = torch.utils.data.DataLoader(test_data, batch_size=50, shuffle=True)
        vae.load_state_dict(torch.load(args.load_path))
        vae.eval()
        with torch.no_grad():
            test(vae, test_loader, "TEST", args)
            au, au_var = calc_au(vae, test_loader)
            print("%d active units" % au)
            # print(au_var)

            calc_iwnll(vae, test_loader, args)

        return

    enc_optimizer = optim.Adam(vae.encoder.parameters(), lr=0.001)
    dec_optimizer = optim.Adam(vae.decoder.parameters(), lr=0.001)
    opt_dict['lr'] = 0.001

    iter_ = 0
    best_loss = 1e4
    best_kl = best_nll = best_ppl = 0
    decay_cnt = pre_mi = best_mi = mi_not_improved =0
    aggressive_flag = True if args.aggressive else False
    vae.train()
    start = time.time()

    kl_weight = args.kl_start
    anneal_rate = (1.0 - args.kl_start) / (args.warm_up * len(train_loader))

    for epoch in range(args.epochs):
        report_kl_loss = report_rec_loss = 0
        report_num_examples = 0
        for datum in train_loader:
            batch_data, _ = datum
            batch_data = torch.bernoulli(batch_data)
            batch_size = batch_data.size(0)

            report_num_examples += batch_size

            # kl_weight = 1.0
            kl_weight = min(1.0, kl_weight + anneal_rate)

            sub_iter = 1
            batch_data_enc = batch_data
            burn_num_examples = 0
            burn_pre_loss = 1e4
            burn_cur_loss = 0
            while aggressive_flag and sub_iter < 100:

                enc_optimizer.zero_grad()
                dec_optimizer.zero_grad()

                burn_num_examples += batch_data_enc.size(0)
                loss, loss_rc, loss_kl = vae.loss(batch_data_enc, kl_weight, nsamples=args.nsamples)

                burn_cur_loss += loss.sum().item()
                loss = loss.mean(dim=-1)

                loss.backward()
                torch.nn.utils.clip_grad_norm_(vae.parameters(), clip_grad)

                enc_optimizer.step()

                id_ = np.random.choice(x_train.size(0), args.batch_size, replace=False)

                batch_data_enc = torch.bernoulli(x_train[id_])

                if sub_iter % 10 == 0:
                    burn_cur_loss = burn_cur_loss / burn_num_examples
                    if burn_pre_loss - burn_cur_loss < 0:
                        break
                    burn_pre_loss = burn_cur_loss
                    burn_cur_loss = burn_num_examples = 0

                sub_iter += 1

            # print(sub_iter)

            enc_optimizer.zero_grad()
            dec_optimizer.zero_grad()


            loss, loss_rc, loss_kl = vae.loss(batch_data, kl_weight, nsamples=args.nsamples)

            loss = loss.mean(dim=-1)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(vae.parameters(), clip_grad)

            loss_rc = loss_rc.sum()
            loss_kl = loss_kl.sum()

            if not aggressive_flag:
                enc_optimizer.step()

            dec_optimizer.step()

            report_rec_loss += loss_rc.item()
            report_kl_loss += loss_kl.item()

            if iter_ % log_niter == 0:
                train_loss = (report_rec_loss  + report_kl_loss) / report_num_examples
                if aggressive_flag or epoch == 0:
                    vae.eval()
                    with torch.no_grad():
                        mi = calc_mi(vae, val_loader)
                        au, _ = calc_au(vae, val_loader)

                    vae.train()

                    print('epoch: %d, iter: %d, avg_loss: %.4f, kl: %.4f, mi: %.4f, recon: %.4f,' \
                           'au %d, time elapsed %.2fs' %
                           (epoch, iter_, train_loss, report_kl_loss / report_num_examples, mi,
                           report_rec_loss / report_num_examples, au, time.time() - start))
                else:
                     print('epoch: %d, iter: %d, avg_loss: %.4f, kl: %.4f, recon: %.4f,' \
                           'time elapsed %.2fs' %
                           (epoch, iter_, train_loss, report_kl_loss / report_num_examples,
                           report_rec_loss / report_num_examples, time.time() - start))
                sys.stdout.flush()

                report_rec_loss = report_kl_loss = 0
                report_num_examples = 0

            iter_ += 1

            if aggressive_flag and (iter_ % len(train_loader)) == 0:
                vae.eval()
                cur_mi = calc_mi(vae, val_loader)
                vae.train()
                if cur_mi - best_mi < 0:
                    mi_not_improved += 1
                    if mi_not_improved == 5:
                        aggressive_flag = False
                        print("STOP BURNING")

                else:
                    best_mi = cur_mi

                pre_mi = cur_mi

        print('kl weight %.4f' % kl_weight)
        print('epoch: %d, VAL' % epoch)

        vae.eval()

        with torch.no_grad():
            loss, nll, kl = test(vae, val_loader, "VAL", args)
            au, au_var = calc_au(vae, val_loader)
            print("%d active units" % au)
            # print(au_var)

        if loss < best_loss:
            print('update best loss')
            best_loss = loss
            best_nll = nll
            best_kl = kl
            torch.save(vae.state_dict(), args.save_path)

        if loss > best_loss:
            opt_dict["not_improved"] += 1
            if opt_dict["not_improved"] >= decay_epoch:
                opt_dict["best_loss"] = loss
                opt_dict["not_improved"] = 0
                opt_dict["lr"] = opt_dict["lr"] * lr_decay
                vae.load_state_dict(torch.load(args.save_path))
                decay_cnt += 1
                print('new lr: %f' % opt_dict["lr"])
                enc_optimizer = optim.Adam(vae.encoder.parameters(), lr=opt_dict["lr"])
                dec_optimizer = optim.Adam(vae.decoder.parameters(), lr=opt_dict["lr"])
        else:
            opt_dict["not_improved"] = 0
            opt_dict["best_loss"] = loss

        if decay_cnt == max_decay:
            break

        if epoch % args.test_nepoch == 0:
            with torch.no_grad():
                loss, nll, kl = test(vae, test_loader, "TEST", args)

        vae.train()

    # compute importance weighted estimate of log p(x)
    vae.load_state_dict(torch.load(args.save_path))
    vae.eval()
    with torch.no_grad():
        loss, nll, kl = test(vae, test_loader, "TEST", args)
        au, au_var = calc_au(vae, test_loader)
        print("%d active units" % au)
        # print(au_var)

    test_loader = torch.utils.data.DataLoader(test_data, batch_size=50, shuffle=True)

    with torch.no_grad():
        calc_iwnll(vae, test_loader, args)

if __name__ == '__main__':
    args = init_config()
    main(args)
