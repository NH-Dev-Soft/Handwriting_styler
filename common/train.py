import os, glob, time, datetime
import numpy as np
from PIL import Image

import torch
import torch.nn as nn
from torchvision.utils import save_image

from common.dataset import TrainDataProvider
from common.function import init_embedding
from common.models import Encoder, Decoder, Discriminator, Generator
from common.utils import denorm_image, centering_image


class Trainer:
    GPU = torch.cuda.is_available()

    data_dir = './dataset/'
    model_dir = './model_save/'
    fixed_dir = './fixed_sample'
    embeddings = torch.load(os.path.join(fixed_dir, 'EMBEDDINGS.pkl'))
    fixed_source = torch.load(os.path.join(fixed_dir, 'fixed_source_all.pkl'))
    fixed_target = torch.load(os.path.join(fixed_dir, 'fixed_target_all.pkl'))
    fixed_label = torch.load(os.path.join(fixed_dir, 'fixed_label_all.pkl'))

    FONTS_NUM = 25
    EMBEDDING_NUM = 100
    BATCH_SIZE = 16
    IMG_SIZE = 128
    EMBEDDING_DIM = 128


    def train(max_epoch, schedule, data_dir, save_path, to_model_path, lr=0.001, \
              log_step=100, sample_step=350, fine_tune=False, flip_labels=False, \
              restore=None, from_model_path=False, GPU=True):

        # Fine Tuning coefficient
        if not fine_tune:
            L1_penalty, Lconst_penalty = 100, 15
        else:
            L1_penalty, Lconst_penalty = 500, 1000

        # Get Models
        En = Encoder()
        De = Decoder()
        D = Discriminator(category_num=FONTS_NUM)
        if GPU:
            En.cuda()
            De.cuda()
            D.cuda()

        # Use pre-trained Model
        # restore에 [encoder_path, decoder_path, discriminator_path] 형태로 인자 넣기
        if restore:
            encoder_path, decoder_path, discriminator_path = restore
            prev_epoch = int(encoder_path.split('-')[0])
            En.load_state_dict(torch.load(os.path.join(from_model_path, encoder_path)))
            De.load_state_dict(torch.load(os.path.join(from_model_path, decoder_path)))
            D.load_state_dict(torch.load(os.path.join(from_model_path, discriminator_path)))
            print("%d epoch trained model has restored" % prev_epoch)
        else:
            prev_epoch = 0
            print("New model training start")


        # L1 loss, binary real/fake loss, category loss, constant loss
        if GPU:
            l1_criterion = nn.L1Loss(size_average=True).cuda()
            bce_criterion = nn.BCEWithLogitsLoss(size_average=True).cuda()
            mse_criterion = nn.MSELoss(size_average=True).cuda()
        else:
            l1_criterion = nn.L1Loss(size_average=True)
            bce_criterion = nn.BCEWithLogitsLoss(size_average=True)
            mse_criterion = nn.MSELoss(size_average=True)


        # optimizer
        G_parameters = list(En.parameters()) + list(De.parameters())
        g_optimizer = torch.optim.Adam(G_parameters, betas=(0.5, 0.999))
        d_optimizer = torch.optim.Adam(D.parameters(), betas=(0.5, 0.999))

        # losses lists
        l1_losses, const_losses, category_losses, d_losses, g_losses = list(), list(), list(), list(), list()

        # training
        count = 0
        for epoch in range(max_epoch):
            if (epoch + 1) % schedule == 0:
                updated_lr = max(lr/2, 0.0002)
                for param_group in d_optimizer.param_groups:
                    param_group['lr'] = updated_lr
                for param_group in g_optimizer.param_groups:
                    param_group['lr'] = updated_lr
                if lr !=  updated_lr:
                    print("decay learning rate from %.5f to %.5f" % (lr, updated_lr))
                lr = updated_lr

            train_batch_iter = data_provider.get_train_iter(BATCH_SIZE)   
            for i, batch in enumerate(train_batch_iter):
                labels, batch_images = batch
                embedding_ids = labels
                if GPU:
                    batch_images = batch_images.cuda()
                if flip_labels:
                    np.random.shuffle(embedding_ids)

                # target / source images
                real_target = batch_images[:, 0, :, :].view([BATCH_SIZE, 1, IMG_SIZE, IMG_SIZE])
                real_source = batch_images[:, 1, :, :].view([BATCH_SIZE, 1, IMG_SIZE, IMG_SIZE])

                # generate fake image form source image
                fake_target, encoded_source = Generator(real_source, En, De, embeddings, embedding_ids, GPU=GPU)

                real_TS = torch.cat([real_source, real_target], dim=1)
                fake_TS = torch.cat([real_source, fake_target], dim=1)

                # Scoring with Discriminator
                real_score, real_score_logit, real_cat_logit = D(real_TS)
                fake_score, fake_score_logit, fake_cat_logit = D(fake_TS)

                # Get encoded fake image to calculate constant loss
                encoded_fake = En(fake_target)[0]
                const_loss = Lconst_penalty * mse_criterion(encoded_source, encoded_fake)

                # category loss
                real_category = torch.from_numpy(np.eye(FONTS_NUM)[embedding_ids]).float()
                if GPU:
                    real_category = real_category.cuda()
                real_category_loss = bce_criterion(real_cat_logit, real_category)
                fake_category_loss = bce_criterion(fake_cat_logit, real_category)
                category_loss = 0.5 * (real_category_loss + fake_category_loss)

                # labels
                if GPU:
                    one_labels = torch.ones([BATCH_SIZE, 1]).cuda()
                    zero_labels = torch.zeros([BATCH_SIZE, 1]).cuda()
                else:
                    one_labels = torch.ones([BATCH_SIZE, 1])
                    zero_labels = torch.zeros([BATCH_SIZE, 1])

                # binary loss - T/F
                real_binary_loss = bce_criterion(real_score_logit, one_labels)
                fake_binary_loss = bce_criterion(fake_score_logit, zero_labels)
                binary_loss = real_binary_loss + fake_binary_loss

                # L1 loss between real and fake images
                l1_loss = L1_penalty * l1_criterion(real_target, fake_target)

                # cheat loss for generator to fool discriminator
                cheat_loss = bce_criterion(fake_score_logit, one_labels)

                # g_loss, d_loss
                g_loss = cheat_loss + l1_loss + fake_category_loss + const_loss
                d_loss = binary_loss + category_loss

                # train Discriminator
                D.zero_grad()
                d_loss.backward(retain_graph=True)
                d_optimizer.step()

                # train Generator
                En.zero_grad()
                De.zero_grad()
                g_loss.backward(retain_graph=True)
                g_optimizer.step()            

                # loss data
                l1_losses.append(l1_loss.data)
                const_losses.append(const_loss.data)
                category_losses.append(category_loss.data)
                d_losses.append(d_loss.data)
                g_losses.append(g_loss.data)

                # logging
                if (i+1) % log_step == 0:
                    time_ = time.time()
                    time_stamp = datetime.datetime.fromtimestamp(time_).strftime('%H:%M:%S')
                    log_format = 'Epoch [%d/%d], step [%d/%d], l1_loss: %.4f, d_loss: %.4f, g_loss: %.4f' % \
                                 (int(prev_epoch)+epoch+1, int(prev_epoch)+max_epoch, i+1, total_batches, \
                                  l1_loss.item(), d_loss.item(), g_loss.item())
                    print(time_stamp, log_format)

                # save image
                if (i+1) % sample_step == 0:
                    fixed_fake_images = Generator(fixed_source, En, De, embeddings, fixed_label, GPU=GPU)[0]
                    save_image(denorm_image(fixed_fake_images.data), \
                               os.path.join(save_path, 'fake_samples-%d-%d.png' % (int(prev_epoch)+epoch+1, i+1)), \
                               nrow=8)

            if (epoch+1) % 5 == 0:
                now = datetime.datetime.now()
                now_date = now.strftime("%m%d")
                now_time = now.strftime('%H:%M')
                torch.save(En.state_dict(), os.path.join(to_model_path, '%d-%s-%s-Encoder.pkl' \
                                                         % (int(prev_epoch)+epoch+1, now_date, now_time)))
                torch.save(De.state_dict(), os.path.join(to_model_path, '%d-%s-%s-Decoder.pkl' % \
                                                         (int(prev_epoch)+epoch+1, now_date, now_time)))
                torch.save(D.state_dict(), os.path.join(to_model_path, '%d-%s-%s-Discriminator.pkl' % \
                                                        (int(prev_epoch)+epoch+1, now_date, now_time)))

        # save model
        total_epoch = int(prev_epoch) + int(max_epoch)
        end = datetime.datetime.now()
        end_date = end.strftime("%m%d")
        end_time = end.strftime('%H:%M')
        torch.save(En.state_dict(), os.path.join(to_model_path, \
                                                 '%d-%s-%s-Encoder.pkl' % (total_epoch, end_date, end_time)))
        torch.save(De.state_dict(), os.path.join(to_model_path, \
                                                 '%d-%s-%s-Decoder.pkl' % (total_epoch, end_date, end_time)))
        torch.save(D.state_dict(), os.path.join(to_model_path, \
                                                '%d-%s-%s-Discriminator.pkl' % (total_epoch, end_date, end_time)))
        losses = [l1_losses, const_losses, category_losses, d_losses, g_losses]
        torch.save(losses, os.path.join(to_model_path, '%d-losses.pkl' % total_epoch))

        return l1_losses, const_losses, category_losses, d_losses, g_losses


def interpolation(data_provider, grids, fixed_char_ids, interpolated_font_ids, embeddings, \
                  En, De, batch_size, img_size=128, save_path=False, GPU=True):
    
    train_batch_iter = data_provider.get_train_iter(batch_size, with_charid=True)
    
    for grid_idx, grid in enumerate(grids):
        train_batch_iter = data_provider.get_train_iter(batch_size, with_charid=True)
        grid_results = {from_to: {charid: None for charid in fixed_char_ids} \
                        for from_to in interpolated_font_ids}

        # total_batch=24, 24번 도는 for문
        for i, batch in enumerate(train_batch_iter):

            # batch_size=18, batch에는 18개의 data 포함
            font_ids_from, char_ids, batch_images = batch
            font_filter = [i[0] for i in interpolated_font_ids]
            font_filter_plus = font_filter + [font_filter[0]]
            font_ids_to = [font_filter_plus[font_filter.index(i)+1] for i in font_ids_from]
            batch_images = batch_images.cuda()

            # 18개의 source/target_from/target_to images 추출
            # source는 같아서 from/to가 나뉘지 않는다
            real_sources = batch_images[:, 1, :, :].view([batch_size, 1, img_size, img_size])
            real_targets = batch_images[:, 0, :, :].view([batch_size, 1, img_size, img_size])

            # real_sources 를 전부 centering / image size fitting 해준다
            for idx, image in enumerate(real_sources):
                image = image.cpu().detach().numpy().reshape(img_size, img_size)
                image = centering_image(image, resize_fix_h=100)
                real_sources[idx] = torch.tensor(image).view([1, img_size, img_size])
                
            # 18개의 encoded_source 생성
            # encoded_source, encode_layer는 interpolate할 필요 없다
            encoded_source, encode_layers = En(real_sources)

            # embedding interpolation 생성
            interpolated_embeddings = []
            embedding_dim = embeddings.shape[3]
            for from_, to_ in zip(font_ids_from, font_ids_to):
                interpolated_embeddings.append((embeddings[from_] * (1 - grid) + \
                                                embeddings[to_] * grid).cpu().numpy())
            interpolated_embeddings = torch.tensor(interpolated_embeddings).cuda()
            interpolated_embeddings = interpolated_embeddings.reshape(batch_size, embedding_dim, 1, 1)

            # generate fake image with embedded source
            interpolated_embedded = torch.cat((encoded_source, interpolated_embeddings), 1)
            fake_targets = De(interpolated_embedded, encode_layers)

            # grid_results에 결과 저장
            # [(0)real_S, (1)real_T, (2)fake_T]
            for fontid, charid, real_S, real_T, fake_T in zip(font_ids_from, char_ids, \
                                                              real_sources, real_targets, \
                                                              fake_targets):
                font_from = fontid
                font_to = font_filter_plus[font_filter.index(fontid)+1]
                from_to = (font_from, font_to)
                grid_results[from_to][charid] = [real_S, real_T, fake_T]

        if save_path:
            for from_to in grid_results.keys():
                image = [grid_results[from_to][charid][2].cpu().detach().numpy() for \
                         charid in fixed_char_ids]
                image = torch.tensor(np.array(image))

                # path
                font_from = str(from_to[0])
                font_to = str(from_to[1])
                grid_idx = str(grid_idx)
                if len(font_from) == 1:
                    font_from = '0' + font_from
                if len(font_to) == 1:
                    font_to = '0' + font_to
                if len(grid_idx) == 1:
                    grid_idx = '0' + grid_idx
                idx = str(interpolated_font_ids.index(from_to))
                if len(idx) == 1:
                    idx = '0' + idx
                file_path = '%s_from_%s_to_%s_grid_%s.png' % (idx, font_from, font_to, grid_idx)

                # save
                save_image(denorm_image(image.data), \
                           os.path.join(save_path, file_path), \
                           nrow=6, pad_value=255)
    
    return grid_results