import torch
import torchvision
from torchvision import models, transforms
import torch.nn as nn
import torch.nn.functional as F
from pytorch_msssim import ssim, ms_ssim, SSIM, MS_SSIM
from torch.autograd import Variable
import numpy as np
from math import exp


def tv_loss(x, beta = 0.5, reg_coeff = 5):
    '''Calculates TV loss for an image `x`.
        
    Args:
        x: image, torch.Variable of torch.Tensor
        beta: See https://arxiv.org/abs/1412.0035 (fig. 2) to see effect of `beta` 
    '''
    dh = torch.pow(x[:,:,:,1:] - x[:,:,:,:-1], 2)
    dw = torch.pow(x[:,:,1:,:] - x[:,:,:-1,:], 2)
    a,b,c,d=x.shape
    return reg_coeff*(torch.sum(torch.pow(dh[:, :, :-1] + dw[:, :, :, :-1], beta))/(a*b*c*d))

class TVLoss(nn.Module):
    def __init__(self, tv_loss_weight=1):
        super(TVLoss, self).__init__()
        self.tv_loss_weight = tv_loss_weight

    def forward(self, x):
        batch_size = x.size()[0]
        h_x = x.size()[2]
        w_x = x.size()[3]
        count_h = self.tensor_size(x[:, :, 1:, :])
        count_w = self.tensor_size(x[:, :, :, 1:])
        h_tv = torch.pow((x[:, :, 1:, :] - x[:, :, :h_x - 1, :]), 2).sum()
        w_tv = torch.pow((x[:, :, :, 1:] - x[:, :, :, :w_x - 1]), 2).sum()
        return self.tv_loss_weight * 2 * (h_tv / count_h + w_tv / count_w) / batch_size

    @staticmethod
    def tensor_size(t):
        return t.size()[1] * t.size()[2] * t.size()[3]


class CharbonnierLoss(nn.Module):
    """Charbonnier Loss (L1)"""

    def __init__(self, eps=1e-3):
        super(CharbonnierLoss, self).__init__()
        self.eps = eps

    def forward(self, x, y):
        diff = x - y
        # loss = torch.sum(torch.sqrt(diff * diff + self.eps))
        loss = torch.mean(torch.sqrt((diff * diff) + (self.eps*self.eps)))
        return loss


class VGGPerceptualLoss(nn.Module):
    def __init__(self, resize=True):
        super(VGGPerceptualLoss, self).__init__()
        blocks = []
        blocks.append(torchvision.models.vgg16(pretrained=True).features[:4].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).features[4:9].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).features[9:16].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).features[16:23].eval())
        for bl in blocks:
            for p in bl.parameters():
                p.requires_grad = False
        self.blocks = torch.nn.ModuleList(blocks)
        self.transform = torch.nn.functional.interpolate
        self.resize = resize
        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def forward(self, input, target, feature_layers=[0, 1, 2, 3], style_layers=[]):
        if input.shape[1] != 3:
            input = input.repeat(1, 3, 1, 1)
            target = target.repeat(1, 3, 1, 1)
        input = (input-self.mean) / self.std
        target = (target-self.mean) / self.std
        if self.resize:
            input = self.transform(input, mode='bilinear', size=(224, 224), align_corners=False)
            target = self.transform(target, mode='bilinear', size=(224, 224), align_corners=False)
        loss = 0.0
        x = input
        y = target
        for i, block in enumerate(self.blocks):
            x = block(x)
            y = block(y)
            if i in feature_layers:
                loss += torch.nn.functional.l1_loss(x, y)
            if i in style_layers:
                act_x = x.reshape(x.shape[0], x.shape[1], -1)
                act_y = y.reshape(y.shape[0], y.shape[1], -1)
                gram_x = act_x @ act_x.permute(0, 2, 1)
                gram_y = act_y @ act_y.permute(0, 2, 1)
                loss += torch.nn.functional.l1_loss(gram_x, gram_y)
        return loss


class VGGLoss(nn.Module):
    """Computes the VGG perceptual loss between two batches of images.
    The input and target must be 4D tensors with three channels
    ``(B, 3, H, W)`` and must have equivalent shapes. Pixel values should be
    normalized to the range 0???1.
    The VGG perceptual loss is the mean squared difference between the features
    computed for the input and target at layer :attr:`layer` (default 8, or
    ``relu2_2``) of the pretrained model specified by :attr:`model` (either
    ``'vgg16'`` (default) or ``'vgg19'``).
    If :attr:`shift` is nonzero, a random shift of at most :attr:`shift`
    pixels in both height and width will be applied to all images in the input
    and target. The shift will only be applied when the loss function is in
    training mode, and will not be applied if a precomputed feature map is
    supplied as the target.
    :attr:`reduction` can be set to ``'mean'``, ``'sum'``, or ``'none'``
    similarly to the loss functions in :mod:`torch.nn`. The default is
    ``'mean'``.
    :meth:`get_features()` may be used to precompute the features for the
    target, to speed up the case where inputs are compared against the same
    target over and over. To use the precomputed features, pass them in as
    :attr:`target` and set :attr:`target_is_features` to :code:`True`.
    Instances of :class:`VGGLoss` must be manually converted to the same
    device and dtype as their inputs.
    """

    models = {'vgg16': models.vgg16, 'vgg19': models.vgg19}

    def __init__(self, model='vgg16', layer=8, shift=0, reduction='mean'):
        super().__init__()
        self.shift = shift
        self.reduction = reduction
        self.normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                              std=[0.229, 0.224, 0.225])
        self.model = self.models[model](pretrained=True).features[:layer+1]
        self.model.eval()
        self.model.requires_grad_(False)

    def get_features(self, input):
        return self.model(self.normalize(input))

    def train(self, mode=True):
        self.training = mode

    def forward(self, input, target, target_is_features=False):
        if target_is_features:
            input_feats = self.get_features(input)
            target_feats = target
        else:
            sep = input.shape[0]
            batch = torch.cat([input, target])
            if self.shift and self.training:
                padded = F.pad(batch, [self.shift] * 4, mode='replicate')
                batch = transforms.RandomCrop(batch.shape[2:])(padded)
            feats = self.get_features(batch)
            input_feats, target_feats = feats[:sep], feats[sep:]
        return F.mse_loss(input_feats, target_feats, reduction=self.reduction)



class ExposureLoss(nn.Module):
    """Exposure Loss (exp)"""

    def __init__(self, patch_size=16,mean_val=0.7):
        super(ExposureLoss, self).__init__()
        self.patch_size = patch_size
        self.mean_val = mean_val

    def forward(self, x):
        b,c,h,w = x.shape
        x = torch.mean(x,1,keepdim=True)
        mean = self.pool(x)

        d = torch.mean(torch.pow(mean- torch.FloatTensor([self.mean_val] ).cuda(),2))
        return torch.mean(d)


class ColorLoss(nn.Module):
    def __init__(self):
        super(ColorLoss, self).__init__()
        # print(1)
    def forward(self, x ):
        # self.grad = np.ones(x.shape,dtype=np.float32)
        b,c,h,w = x.shape
        # x_de = x.cpu().detach().numpy()
        r,g,b = torch.split(x , 1, dim=1)
        mean_rgb = torch.mean(x,[2,3],keepdim=True)
        mr,mg, mb = torch.split(mean_rgb, 1, dim=1)
        Dr = r-mr
        Dg = g-mg
        Db = b-mb
        k =torch.pow( torch.pow(Dr,2) + torch.pow(Db,2) + torch.pow(Dg,2),0.5)
        # print(k)
        

        k = torch.mean(k)
        return k


class ColorLoss1(nn.Module):
    def __init__(self):
        super(ColorLoss1, self).__init__()
    def forward(self, x ,y  ):
        b,c,h,w = x.shape
        #????????????????????????????????????
        #??????rgb????????????
        r1,g1,b1 = torch.split(x, 1, dim=1)
        r2,g2,b2 = torch.spiit(y, 1, dim=1)

        # r1 = r1*0.3
        # g1 = g1*0.59
        # b1 = b1*0.11
        # r2 = r2*0.3
        # g2 = g2*0.59
        # b2 = b2*0.11

        k1 = r1*r2 + g1*g2 + b1*b2  
        k2 = torch.pow( torch.pow(r1,2) + torch.pow(g1,2) + torch.pow(b1,2),0.5)
        k3 = torch.pow( torch.pow(r2,2) + torch.pow(g2,2) + torch.pow(b2,2),0.5)
        # k =torch.pow( torch.pow(Dr,2) + torch.pow(Db,2) + torch.pow(Dg,2),0.5)
        # print(k)
        cos = k1 / (k2*k3)
        

        k = torch.mean(torch.arcos(cos))
        return k

class UnionLoss(nn.Module):
    def __init__(self):
        super(UnionLoss, self).__init__()
    def forward(self, x ,y):
        b,c,h,w = x.shape
     
        x_roll2 = torch.roll(x,2,2)
        y_roll2 = torch.roll(y,2,2)
        x_roll4 = torch.roll(x,4,2)
        y_roll4 = torch.roll(y,4,2)
        x_roll8 = torch.roll(x,8,2)
        y_roll8 = torch.roll(y,8,2)
        x_roll16 = torch.roll(x,16,2)
        y_roll16 = torch.roll(y,16,2)
        x_roll32 = torch.roll(x,32,2)
        y_roll32 = torch.roll(y,32,2)
        x_roll64 = torch.roll(x,64,2)
        y_roll64 = torch.roll(y,64,2)
        x_roll128 = torch.roll(x,128,2)
        y_roll128 = torch.roll(y,128,2)

        x_ = torch.abs(x_roll2-x) + torch.abs(x_roll4-x) + torch.abs(x_roll8-x) + torch.abs(x_roll16-x) + torch.abs(x_roll32-x) + torch.abs(x_roll64-x) + torch.abs(x_roll128-x)
        y_ = torch.abs(y_roll2-y) + torch.abs(y_roll4-y) + torch.abs(y_roll8-y) + torch.abs(y_roll16-y) + torch.abs(y_roll32-y) + torch.abs(y_roll64-y) + torch.abs(y_roll128-y)

        x_roll2 = torch.roll(x,2,3)
        y_roll2 = torch.roll(y,2,3)
        x_roll4 = torch.roll(x,4,3)
        y_roll4 = torch.roll(y,4,3)
        x_roll8 = torch.roll(x,8,3)
        y_roll8 = torch.roll(y,8,3)
        x_roll16 = torch.roll(x,16,3)
        y_roll16 = torch.roll(y,16,3)
        x_roll32 = torch.roll(x,32,3)
        y_roll32 = torch.roll(y,32,3)
        x_roll64 = torch.roll(x,64,3)
        y_roll64 = torch.roll(y,64,3)
        x_roll128 = torch.roll(x,128,3)
        y_roll128 = torch.roll(y,128,3)
        x__ = torch.abs(x_roll2-x) + torch.abs(x_roll4-x) + torch.abs(x_roll8-x) + torch.abs(x_roll16-x) + torch.abs(x_roll32-x) + torch.abs(x_roll64-x) + torch.abs(x_roll128-x)
        y__ = torch.abs(y_roll2-y) + torch.abs(y_roll4-y) + torch.abs(y_roll8-y) + torch.abs(y_roll16-y) + torch.abs(y_roll32-y) + torch.abs(y_roll64-y) + torch.abs(y_roll128-y)

        
        k = torch.mean(torch.abs(x_ - y_)/7 + torch.abs(x__ - y__)/7 )/2
        return k

class MixLoss(nn.Module):
    def __init__(self, alpha=0.8, eps=1e-6, window_size = 11, size_average = True):
        super(MixLoss, self).__init__()
        self.eps = eps
        self.alpha = alpha
        # self.window_size = window_size
        # self.size_average = size_average
        # self.channel = 1
        # self.window = create_window(window_size, self.channel)

    def forward(self, x, y, epoch=None):
        """Charbonnier Loss (L1) ???0-1???"""  
        diff = x - y
        l1_loss = torch.mean(torch.sqrt((diff * diff) + (self.eps*self.eps))) #10->5

        """SSIM ???0-1???"""
        ssim_module = SSIM(data_range=1., size_average=True, channel=3)
        ssim_loss = 1 - ssim_module(x, y)
        loss =  (1-self.alpha)*l1_loss + self.alpha*ssim_loss  
        return loss
class MyLoss(nn.Module):
    
    def __init__(self):
        super(MyLoss, self).__init__()
        self.l1_module = CharbonnierLoss()
        self.ssim_module = SSIM(data_range=1.0, size_average=True, channel=3,nonnegative_ssim=True)
        self.tv_module = TVLoss()
        self.exp_module = ExposureLoss(16, 0.7)
        self.color_module = ColorLoss1()
        self.union_module = UnionLoss()
        self.ms_ssim_module = MS_SSIM(data_range=1.0, size_average=True, channel=3, win_size=7)


    def forward(self, x, y,epoch):
        l1_loss = self.l1_module(x,y)
        ssim_loss =  (1 - self.ssim_module(x, y)) #100 ssim:50-7
        # # ms_ssim_loss = 1000*(1 - self.ms_ssim_module(x,y)) #1000 ms-ssim:48 -> 3.4
        # tv_loss = self.tv_module(x)
        # exp_loss = self.exp_module(x)
        # color_loss = self.color_module(x,y)
        # union_loss = self.union_module(x,y)
        # if epoch>100 :
        #     vgg_module = VGGPerceptualLoss().cuda()
        #     vgg_loss = vgg_module(x,y)
        #     loss =  l1_loss + ssim_loss + vgg_loss
        # else :
        #     loss =  l1_loss + ssim_loss

        # loss = l1_loss + 80 * ssim_loss + 10 * tv_loss + color_loss
        loss = l1_loss + 0.01 * ssim_loss
        if(epoch%3==1):
            print("l1_loss:" ,l1_loss.item() ,"ssim_loss", ssim_loss)


        return loss