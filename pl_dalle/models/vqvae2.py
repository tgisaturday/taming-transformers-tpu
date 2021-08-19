import torch
from torch import nn
from torch.nn import functional as F
import pytorch_lightning as pl
from torch import distributed as dist
from torch.optim.lr_scheduler import ReduceLROnPlateau
import math
from einops import rearrange

from pl_dalle.modules.losses.patch import PatchReconstructionDiscriminator

#from torch import distributed as dist
# import vqvae.distributed as dist_fn

# Copyright 2018 The Sonnet Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================


# Borrowed from https://github.com/deepmind/sonnet and ported it to PyTorch

class VQVAE2(pl.LightningModule):
    def __init__(self,
                 args, batch_size, learning_rate,
                 ignore_keys=[],
                 stride_1=8,
                 stride_2=2,
                 ):
        super().__init__()
        self.save_hyperparameters()
        self.args = args  
        self.recon_loss = nn.MSELoss()
        self.latent_loss_weight = args.quant_beta      
        self.image_size = args.resolution
        self.num_tokens = args.num_tokens * 2 #two codebooks

        self.enc_b = Encoder(args.in_channels, args.hidden_dim, args.num_res_blocks, args.num_res_ch, stride=stride_1)
        self.enc_t = Encoder(args.hidden_dim, args.hidden_dim, args.num_res_blocks, args.num_res_ch, stride=stride_2)
        self.quantize_conv_t = nn.Conv2d(args.hidden_dim, args.codebook_dim, 1)
        self.quantize_t = Quantize(args.codebook_dim, args.num_tokens, args.quant_ema_decay)
        self.dec_t = Decoder(
            args.codebook_dim, args.codebook_dim, args.hidden_dim, args.num_res_blocks, args.num_res_ch, stride=stride_2
        )
        self.quantize_conv_b = nn.Conv2d(args.codebook_dim + args.hidden_dim, args.codebook_dim, 1)
        self.quantize_b = Quantize(args.codebook_dim, args.num_tokens, args.quant_ema_decay)
        self.upsample_t = nn.ConvTranspose2d(
            args.codebook_dim, args.codebook_dim, 4, stride=2, padding=1
        )
        self.dec = Decoder(
            args.codebook_dim + args.codebook_dim,
            args.in_channels,
            args.hidden_dim,
            args.num_res_blocks,
            args.num_res_ch,
            stride=stride_1,
        )
        self.image_seq_len = (self.image_size // 8) ** 2 + (self.image_size // 16) ** 2

    def forward(self, input):
        quant_t, quant_b, diff, _, _ = self.encode(input)
        dec = self.decode(quant_t, quant_b)

        return dec, diff

    def encode(self, input):
        enc_b = self.enc_b(input)
        enc_t = self.enc_t(enc_b)

        quant_t = self.quantize_conv_t(enc_t).permute(0, 2, 3, 1)
        quant_t, diff_t, id_t = self.quantize_t(quant_t)
        quant_t = quant_t.permute(0, 3, 1, 2)
        diff_t = diff_t.unsqueeze(0)

        dec_t = self.dec_t(quant_t)
        enc_b = torch.cat([dec_t, enc_b], 1)

        quant_b = self.quantize_conv_b(enc_b).permute(0, 2, 3, 1)
        quant_b, diff_b, id_b = self.quantize_b(quant_b)
        quant_b = quant_b.permute(0, 3, 1, 2)
        diff_b = diff_b.unsqueeze(0)

        return quant_t, quant_b, diff_t + diff_b, id_t, id_b

    def decode(self, quant_t, quant_b):
        upsample_t = self.upsample_t(quant_t)
        quant = torch.cat([upsample_t, quant_b], 1)
        dec = self.dec(quant)

        return dec


    @torch.no_grad()
    def get_codebook_indices(self, img):
        b = img.shape[0]
        img = (2 * img) - 1
        _, _, _, id_t, id_b = self.encode(img)
        #id_t = rearrange(id_t, 'b h w -> b (h w)', b=b)
        id_t = id_t.view(b,-1)
        #id_b = rearrange(id_b, 'b h w -> b (h w)', b=b)
        id_b = id_b.view(b,-1)
        indices = torch.cat((id_t,id_b),1)        
        return indices

    def training_step(self, batch, batch_idx):         
        x = batch[0]
        xrec, qloss = self(x)

        recon_loss = self.recon_loss(xrec, x)
        latent_loss = qloss.mean()
        loss = recon_loss + self.latent_loss_weight * latent_loss

        self.log("train/rec_loss", recon_loss, prog_bar=True, logger=True)
        self.log("train/embed_loss", latent_loss, prog_bar=True, logger=True)
        self.log("train/total_loss", loss, prog_bar=True, logger=True)                

        if self.args.log_images:
            return {'loss': loss, 'x': x.detach(), 'xrec': xrec.detach()}
        else:
            return loss

    def validation_step(self, batch, batch_idx):
        x = batch[0]
        xrec, qloss = self(x)

        recon_loss = self.recon_loss(xrec, x)
        latent_loss = qloss.mean()
        loss = recon_loss + self.latent_loss_weight * latent_loss
        
        self.log("val/rec_loss", recon_loss, prog_bar=True, logger=True)
        self.log("val/embed_loss", latent_loss, prog_bar=True, logger=True)
        self.log("val/total_loss", loss, prog_bar=True, logger=True)  
           
        if self.args.log_images:
            return {'loss':loss, 'x':x.detach(), 'xrec':xrec.detach()}
        return loss

    def configure_optimizers(self):
        lr = self.hparams.learning_rate
        opt = torch.optim.Adam(self.parameters(),lr=lr, betas=(0.5, 0.9))
        if self.args.lr_decay:
            scheduler = ReduceLROnPlateau(
            opt,
            mode="min",
            factor=0.5,
            patience=10,
            cooldown=10,
            min_lr=1e-6,
            verbose=True,
            )    
            sched = {'scheduler':scheduler, 'monitor':'val/total_loss'}                
            return [opt], [sched]
        else:
            return [opt], []   


class VQGAN2(VQVAE2):
    def __init__(self, *args, patch_sizes=[4, 8, 16], **kwargs):
        super().__init__(*args, **kwargs)
        self.discriminators = []
        for size in patch_sizes:
            self.discriminators.append(
                PatchReconstructionDiscriminator(
                    size,
                    self.args.in_channel,
                    self.args.hidden_dim,
                    min(4, size // 4),
                    self.args.hidden_dim * 2,
                )
            )

    def configure_optimizers(self):
        lr = self.hparams.learning_rate
        g_params = \
            [self.enc_b.parameters()] + \
            [self.enc_t.parameters()] + \
            [self.dec_t.parameters()] + \
            [self.dec.parameters()] + \
            [self.quantize_t.paramters()] + \
            [self.quantize_b.parameters()] + \
            [self.quantize_conv_t.parameters()] + \
            [self.quantize_conv_b.parameters()] + \
            [self.usample_t.parameters()] + \
            []
        d_params = []
        for d in self.discriminators:
            d_params.extend([d.parameters()])

        opt_g = torch.optim.Adam(g_params, lr=lr, betas=(0.5, 0.9))
        opt_d = torch.optim.Adam(d_params, lr=lr, betas=(0.5, 0.9))
        if self.args.lr_decay:
            scheduler = ReduceLROnPlateau(
                opt_g,
                mode="min",
                factor=0.5,
                patience=10,
                cooldown=10,
                min_lr=1e-6,
                verbose=True,
            )
            sched = {'scheduler': scheduler, 'monitor': 'val/total_loss'}
            return [opt_g, opt_d], [sched]
        else:
            return [opt_g, opt_d], []

    def training_step(self, batch, batch_idx, optimizer_idx):
        x = batch[0]
        xrec, qloss = self(x)

        if optimizer_idx == 0:
            recon_loss = self.recon_loss(xrec, x)
            latent_loss = qloss.mean()
            g_loss = sum(d.g_loss(x, xrec) for d in self.discriminators)
            loss = recon_loss + self.latent_loss_weight * latent_loss + g_loss
            self.log("train/rec_loss", recon_loss, prog_bar=True, logger=True)
            self.log("train/embed_loss", latent_loss, prog_bar=True, logger=True)
            self.log("train/g_loss", g_loss, prog_bar=True, logger=True)
            self.log("train/total_loss", loss, prog_bar=True, logger=True)
        elif optimizer_idx == 1:
            d_loss = sum(d.d_loss(x, xrec) for d in self.discriminators)
            loss = d_loss
            self.log('train/d_loss', d_loss, prog_bar=True, logger=True)

        if self.args.log_images:
            return {'loss': loss, 'x': x.detach(), 'xrec': xrec.detach()}
        else:
            return loss


class Quantize(nn.Module):
    def __init__(self, dim, num_tokens, decay=0.99, eps=1e-5):
        super().__init__()

        self.dim = dim
        self.num_tokens = num_tokens
        self.decay = decay
        self.eps = eps

        embed = torch.randn(dim, num_tokens)
        self.register_buffer("embed", embed)
        self.register_buffer("cluster_size", torch.zeros(num_tokens))
        self.register_buffer("embed_avg", embed.clone())

    def forward(self, input):
        flatten = input.reshape(-1, self.dim)
        dist = (
            flatten.pow(2).sum(1, keepdim=True)
            - 2 * flatten @ self.embed
            + self.embed.pow(2).sum(0, keepdim=True)
        )
        _, embed_ind = (-dist).max(1)
        embed_onehot = F.one_hot(embed_ind, self.num_tokens).type(flatten.dtype)
        embed_ind = embed_ind.view(*input.shape[:-1])
        quantize = self.embed_code(embed_ind)

        if self.training:
            embed_onehot_sum = embed_onehot.sum(0)
            embed_sum = flatten.transpose(0, 1) @ embed_onehot

            #self.all_reduce(embed_onehot_sum)
            #self.all_reduce(embed_sum)

            self.cluster_size.data.mul_(self.decay).add_(
                embed_onehot_sum, alpha=1 - self.decay
            )
            self.embed_avg.data.mul_(self.decay).add_(embed_sum, alpha=1 - self.decay)
            n = self.cluster_size.sum()
            cluster_size = (
                (self.cluster_size + self.eps) / (n + self.num_tokens * self.eps) * n
            )
            embed_normalized = self.embed_avg / cluster_size.unsqueeze(0)
            self.embed.data.copy_(embed_normalized)

        diff = (quantize.detach() - input).pow(2).mean()
        quantize = input + (quantize - input).detach()

        return quantize, diff, embed_ind

    def embed_code(self, embed_id):
        return F.embedding(embed_id, self.embed.transpose(0, 1))


class ResBlock(nn.Module):
    def __init__(self, in_channel, channel):
        super().__init__()

        self.conv = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(in_channel, channel, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel, in_channel, 1),
        )

    def forward(self, input):
        out = self.conv(input)
        out += input

        return out


class Encoder(nn.Module):
    def __init__(self, in_channel, channel, n_res_block, n_res_channel, stride):
        super().__init__()

        blocks = []
        strides = int(math.log2(stride))

        if strides == 0:
            blocks.append(nn.Conv2d(in_channel, channel // 2, 3, padding=1))
            blocks.append(nn.ReLU(inplace=True))

        for i in range(strides):
            # first stride
            if i == 0:
                blocks.append(nn.Conv2d(in_channel, channel // 2, 4, stride=2, padding=1))
            # last stride
            elif i + 1 == strides:
                blocks.append(nn.Conv2d(channel // 2, channel, 4, stride=2, padding=1))
            # middle stride
            else:
                blocks.append(nn.Conv2d(channel // 2, channel // 2, 4, stride=2, padding=1))
            blocks.append(nn.ReLU(inplace=True))

        if strides <= 1:
            strides.append(nn.Conv2d(channel // 2, channel, 3, padding=1))
        else:
            strides.append(nn.Conv2d(channel, channel, 3, padding=1))

        for i in range(n_res_block):
            blocks.append(ResBlock(channel, n_res_channel))

        blocks.append(nn.ReLU(inplace=True))

        self.blocks = nn.Sequential(*blocks)

    def forward(self, input):
        return self.blocks(input)


class Decoder(nn.Module):
    def __init__(
        self, in_channel, out_channel, channel, n_res_block, n_res_channel, stride
    ):
        super().__init__()

        blocks = [nn.Conv2d(in_channel, channel, 3, padding=1)]

        for i in range(n_res_block):
            blocks.append(ResBlock(channel, n_res_channel))

        blocks.append(nn.ReLU(inplace=True))

        strides = int(math.log2(stride))
        if strides == 1:
            blocks.append(nn.ConvTranspose2d(channel, out_channel, 4, stride=2, padding=1))
        else:
            for i in range(strides):
                if i == 0:
                    blocks.extend([nn.ConvTranspose2d(channel, channel // 2, 4, stride=2, padding=1), nn.ReLU(inplace=True)])
                elif i + 1 < strides:
                    blocks.extend([nn.ConvTranspose2d(channel // 2, channel // 2, 4, stride=2, padding=1), nn.ReLU(inplace=True)])
                else:
                    blocks.append(nn.ConvTranspose2d(channel // 2, out_channel, 4, stride=2, padding=1))

        self.blocks = nn.Sequential(*blocks)

    def forward(self, input):
        return self.blocks(input)
