import torch
import torch.nn as nn
import torch.nn.functional as F
import os
import json


with open('class_names.json','r') as f:
      classes = json.load(f)
num_class = len(classes)

"""### Layers"""
class Flatten(nn.Module):
  def __init__(self):
    super(Flatten, self).__init__()

  def forward(self, x):
    shape = x.shape
    return x.reshape(shape[0], shape[1]*shape[2]*shape[3])

class Reshape(nn.Module):
  def __init__(self, new_shape):
    super(Reshape, self).__init__()
    self.new_shape = [int(i) for i in new_shape]

  def forward(self, x):
    batch_size = x.shape[0]
    return x.reshape(batch_size, *self.new_shape)

class Hswish(nn.Module):
  def __init__(self, inplace=True):
    super(Hswish, self).__init__()
    self.inplace=inplace

  def forward(self, x):
    return x * F.relu6(x + 3.0, inplace=self.inplace) / 6.0

class Hsigmoid(nn.Module):
  def __init__(self, inplace=True):
    super(Hsigmoid, self).__init__()
    self.inplace = inplace
  
  def forward(self, x):
    return F.relu6(x + 3.0, inplace=self.inplace) / 6.0

class SEBlock(nn.Module):
  def __init__(self, channel, reduction=4):
    super(SEBlock, self).__init__()
    self.pool = nn.AdaptiveAvgPool2d(1)
    self.fc = nn.Sequential(
            nn.Linear(channel, channel//reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel//reduction, channel, bias=False),
            Hsigmoid(),
          )
  def forward(self, x):
    batch_size, channels = x.shape[:2]
    y = self.pool(x).reshape(batch_size, channels)
    y = self.fc(y).reshape(batch_size, channels, 1, 1)
    return x * y.expand_as(x)

class Activation(nn.Module):
  def __init__(self, config, inplace=True):
    super(Activation, self).__init__()
    self.inplace = inplace
    if config in ['r', 'relu']:
      act = nn.ReLU(inplace=inplace)
    elif config in ['s', 'sigmoid']:
      act = nn.Sigmoid(inplace=inplace)
    elif config in ['t', 'tanh']:
      act = nn.Tanh(inplace=inplace)
    elif config in ['r6', 'relu6']:
      act = nn.ReLU6(inplace=inplace)
    elif config in ['e', 'elu']:
      act = nn.ELU(inplace=inplace)
    elif config.startswith('lr') or config.startswith('lrelu'):
      slope = config.replace('lrelu','').replace('lr')
      act = nn.LeakyReLU(negative_slope=float(slope), inplace=inplace)
    elif config in ['selu']:
      act = nn.SELU(inplace=inplace)
    elif config in ['hs','hsigmoid']:
      act = Hsigmoid(inplace=inplace)
    elif config in ['hsw','hswish']:
      act = Hswish(inplace=inplace)
    else:
      raise NotImplementedError(config)
    self.activation = act
  
  def forward(self, x):
    return self.activation(x)

class CBABlock(nn.Module):
  def __init__(self,
         in_channels,
         out_channels,
         kernel_size,
         stride=1,
         padding=0,
         activation='r',
         transpose=0,
         norm='bn',
         ):
    super(CBABlock, self).__init__()
    self.in_channels = int(in_channels)
    self.out_channels = int(out_channels)
    self.kernel_size = int(kernel_size)
    self.stride = int(stride) or 1
    self.padding = int(padding) or 0
    self.activation = activation or 'r'
    self.transpose = int(transpose)
    self.norm = norm
    if self.transpose == 0:
      Layer = nn.Conv2d
    else:
      Layer = nn.ConvTranspose2d
    self.conv = Layer(in_channels=self.in_channels,
              out_channels=self.out_channels,
              kernel_size=self.kernel_size,
              stride=self.stride,
              padding=self.padding)
    if self.norm == 'bn':
      self.bn = nn.BatchNorm2d(num_features=self.out_channels)
    elif self.norm == 'ln':
      self.bn = nn.LayerNorm(normalized_shape=self.out_channels)
    else:
      self.bn = nn.Identity()
    self.activation = Activation(self.activation)

  def forward(self, x):
    x = self.conv(x)
    x = self.bn(x)
    x = self.activation(x)
    return x

class Bottleneck(nn.Module):
  def __init__(self,
         in_c,
         out_c,
         hid_c,
         kernel,
         stride,
         se=False,
         activation='r'):
    super(Bottleneck, self).__init__()
    assert stride in [1,2]
    assert kernel in [3,5]
    padding = (kernel-1)//2
    self.res = stride == 1 and in_c == out_c
    if se:
      SE = SEBlock
    else:
      SE = nn.Identity
    self.layers = nn.Sequential(
        nn.Conv2d(in_c, hid_c, 1, 1, 0, bias=False),
        nn.BatchNorm2d(hid_c),
        Activation(activation, inplace=True),
        nn.Conv2d(hid_c, hid_c, kernel, stride, 
            padding, groups=hid_c,bias=False),
        nn.BatchNorm2d(hid_c),
        SE(hid_c),
        Activation(activation, inplace=True),
        nn.Conv2d(hid_c, out_c, 1, 1, 0, bias=False),
        nn.BatchNorm2d(out_c),
    )

  def forward(self, x):
    y = self.layers(x)
    if self.res:
      return x + y
    else:
      return y

class EasySequential(nn.Module):
  def __init__(self, config):
    super(EasySequential, self).__init__()
    self.layers = []
    self.config = config
    for i in config:
      if i.startswith('l '):
        _, in_dim, out_dim = i.split(' ')
        layer = nn.Linear(int(in_dim), int(out_dim))
      elif i.startswith('c '):
        _, in_c, out_c, kernel, stride, padding = i.split(' ')
        layer = nn.Conv2d(in_channels=int(in_c),
                  out_channels=int(out_c),
                  kernel_size=int(kernel),
                  stride=int(stride),
                  padding=int(padding),
                  )
      elif i.startswith('cba '):
        (_, in_c, out_c, kernel, stride,
         padding, activation, transpose, norm) = i.split(' ')
        layer = CBABlock(in_channels=in_c,
                 out_channels=out_c,
                 kernel_size=kernel,
                 stride=stride,
                 padding=padding,
                 activation=activation,
                 transpose=transpose,
                 norm=norm)
      elif i.startswith('btn '): # bottleneck
        (_, in_c, out_c, hid_c, kernel,
         stride, se, activation) = i.split(' ')
        layer = Bottleneck(int(in_c),
                  int(out_c),
                  int(hid_c),
                  int(kernel),
                  int(stride),
                  bool(int(se)),
                  activation)
      elif i.startswith('ct '): # conv_transposed2d
        _, in_c, out_c, kernel, stride, padding = i.split(' ')
        layer = nn.ConvTranspose2d(in_channels=int(in_c),
                       out_channels=int(out_c),
                       kernel_size=int(kernel),
                       stride=int(stride),
                       padding=int(padding),
                       )
      elif i.startswith('mp '): # maxpool
        _, kernel = i.split(' ')
        layer = nn.MaxPool2d(kernel_size=int(kernel))
      elif i.startswith('ap '): # averagepool
        _, kernel = i.split(' ')
        layer = nn.AvgPool2d(kernel_size=int(kernel))
      elif i.startswith('aap '): # adaptive average pool
        _, kernel = i.split(' ')
        layer = nn.AdaptiveAvgPool2d(output_size=int(kernel))
      elif i.startswith('up '): # upsample
        _, factor = i.split(' ')
        layer = nn.Upsample(scale_factor=int(factor))
      elif i.startswith('bn '):
        _, features = i.split(' ')
        layer = nn.BatchNorm2d(num_features=int(features))
      elif i.startswith('ln '):
        _, features = i.split(' ')
        layer = nn.LayerNorm(num_features=int(features))
      elif i.startswith('d','dropout'):
        _, p = i.split(' ')
        layer = nn.Dropout(float(p))
      elif i in ['flat','flatten']:
        layer = Flatten()
      elif i.startswith('reshape '):
        new_shape = i.split(' ')[1:]
        layer = Reshape(new_shape)
      else:
        layer = Activation(i)
      self.layers.append(layer)
    self.layers = nn.ModuleList(self.layers)

  def forward(self, x):
    for layer in self.layers:
      x = layer(x)
    return x

"""### Core"""

class MobileNetV3(nn.Module):
  def __init__(self, config, num_class, dropout=0.8):
    super(MobileNetV3, self).__init__()
    self.num_class = num_class
    self.layers = EasySequential(config)
    self.classifier = nn.Sequential(
        nn.Dropout(dropout),
        nn.Linear(256, num_class)
    )
    # initialize weights
    for module in self.modules():
      if type(module) is nn.Conv2d:
        nn.init.kaiming_normal_(module.weight, mode='fan_out')
        if module.bias is not None:
          nn.init.zeros_(module.bias)
      elif type(module) is nn.BatchNorm2d:
        nn.init.ones_(module.weight)
        nn.init.zeros_(module.bias)
      elif type(module) is nn.Linear:
        nn.init.normal_(module.weight, 0, 0.01)
        if module.bias is not None:
          nn.init.zeros_(module.bias)

  def forward(self, x):
    y = self.layers(x)
    y = y.mean(3).mean(2)
    y = self.classifier(y)
    return y

'''mobilenet_tiny_0: (Largest)
99% accuracy on training set after 50k steps
96% accuracy with data augmentation and dropout
32batch * 30iteration in 0.93sec CPU
256batch * 30iteration in 5.2sec CPU
512batch * 30iteration in 9.8sec CPU
'''
config0 = [
  'cba 3 16 3 1 0 hsw 0 bn',
  'btn 16 24 16 3 2 1 r',
  'btn 24 24 48 3 2 0 r',
  'btn 24 32 60 3 1 1 hsw',
  'btn 32 48 100 5 1 1 hsw',
  'btn 48 64 128 5 2 1 hsw',
  'cba 64 128 1 1 0 hsw 0 bn',
  'aap 1',
  'cba 128 256 1 1 0 hsw 0 n',
]

'''mobilenet_tiny_1:
99% accuracy on training set after 70k steps
94.3% accuracy with data augmentation and dropout
32batch * 30iteration in 0.65sec CPU
256batch * 30iteration in 4sec CPU
512batch * 30iteration in 7.5sec CPU
'''
config1 = [
  'cba 3 16 3 1 0 hsw 0 bn',
  'btn 16 24 16 3 2 1 r',
  'btn 24 24 48 3 2 0 r',
  'btn 24 32 60 3 1 1 hsw',
  'btn 32 64 128 5 2 1 hsw',
  'cba 64 256 1 1 0 hsw 0 n',
]

'''mobilenet_tiny_2: (Fastest)
99% accuracy on training set after 73k steps
95% accuracy with data augmentation and dropout
32batch * 30iteration in 0.62sec CPU
256batch * 30iteration in 3.2sec CPU
512batch * 30iteration in 6.5sec CPU

'''
config2 = [
  'cba 3 16 3 1 0 hsw 0 bn',
  'btn 16 24 16 3 2 1 r',
  'btn 24 24 32 3 2 0 r',
  'btn 24 32 48 3 1 1 hsw',
  'btn 32 32 60 3 2 1 hsw',
  'btn 32 48 64 5 2 1 hsw',
  'btn 48 48 80 5 2 1 hsw',
  'cba 48 256 1 1 0 hsw 0 n',
]

'''mobilenet_tiny_3: (Best)
_% accuracy on training set after _k steps
_% accuracy with data augmentation and dropout
32batch * 30iteration in 0.65sec CPU
256batch * 30iteration in 3.9sec CPU
512batch * 30iteration in 7.2sec CPU

'''
config3 = [
  'cba 3 16 3 1 0 hsw 0 bn',
  'btn 16 24 16 3 2 1 r',
  'btn 24 24 32 3 2 0 r',
  'btn 24 32 48 3 1 1 hsw',
  'btn 32 32 60 3 2 1 hsw',
  'btn 32 48 80 5 1 1 hsw',
  'btn 48 48 94 5 2 1 hsw',
  'cba 48 128 1 1 0 hsw 0 bn',
  'aap 1',
  'cba 128 256 1 1 0 hsw 0 n',
]

def mobilenet_v3(version, pretrained=True):
  if version == 0:
    config = config0
  elif version == 1:
    config = config1
  elif version == 2:
    config = config2
  elif version == 3:
    config = config3
  elif version == 4:
    config = config3
  else:
    raise NotImplementedError("version doesn't exist")
  model = MobileNetV3(config, num_class=num_class)
  path = f'weights/mobilenet_tiny_{version}.pt'
  if os.path.exists(path):
    model.load_state_dict(torch.load(path, map_location=torch.device('cpu')))
  else:
    print('no pretrained weights found')
  for param in model.parameters():
    param.requires_grad = False
  return model
