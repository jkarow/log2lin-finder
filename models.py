import csv
from typing import Dict, List, Tuple

import torch
from torch import nn
from torch.utils import data
from dataclasses import dataclass
from tqdm import tqdm
import numpy as np

from matplotlib import pyplot as plt
from lut_parser import lut_1d_properties


@dataclass
class exp_parameters_simplified:
    base: float
    offset: float
    scale: float
    slope: float
    intercept: float
    cut: float

    def __str__(self):
        return f"base={self.base:0.3f} offset={self.offset:0.3f} scale={self.scale:0.3f} slope={self.slope:0.3f} intercept={self.intercept:0.3f} cut={self.cut:0.3f}"

    def log_curve_to_str(self):
        output = f"""
const float base = {self.base};
const float offset = {self.offset};
const float scale = {self.scale};
const float slope = {self.slope};
const float intercept = {self.intercept};
const float cut = {self.cut};


const float y_cut = slope * cut + intercept;
if (y < y_cut) {{
    return (y - intercept) / slope;
}} else {{
    return _log10f((y - offset) / scale) / _log10f(base);
}}
        """
        return output

    def exp_curve_to_str(self):
        output = f"""
const float base = {self.base};
const float offset = {self.offset};
const float scale = {self.scale};
const float slope = {self.slope};
const float intercept = {self.intercept};
const float cut = {self.cut};

if (x < cut) {{
    return slope * x + intercept;
}} else {{
    return _powf(base, x) * scale + offset;
}}
"""
        return output

    def to_csv(self):
        output = f"""name,value
base,{self.base}
offset,{self.offset}
scale,{self.scale}
slope,{self.slope}
intercept,{self.intercept}
cut,{self.cut}
"""
        return output

    @staticmethod
    def from_csv(csv_fn: str) -> "exp_parameters_simplified":
        parsed_dict = {}
        with open(csv_fn, 'r') as f:
            dict_reader = csv.DictReader(f)
            for d in dict_reader:
                parsed_dict[d['name']] = float(d['value'])
        return exp_parameters_simplified(
            base=parsed_dict['base'],
            offset=parsed_dict['offset'],
            scale=parsed_dict['scale'],
            slope=parsed_dict['slope'],
            intercept=parsed_dict['intercept'],
            cut=parsed_dict['cut']
        )

INITIAL_GUESS = exp_parameters_simplified(
    base=0.376,
    offset=2.9e-4,
    scale=1.1e-4,
    slope=.005,
    intercept=.00343,
    cut=0.1506,
)

def dataset_from_1d_lut(lut: lut_1d_properties) -> data.dataset:
    x = torch.arange(0, lut.size, dtype=torch.float) * \
        (lut.domain_max[0] - lut.domain_min[0]) / \
        (lut.size - 1) + \
        lut.domain_min[0]
    y = torch.tensor(lut.contents[:, 0], dtype=torch.float)
    return data.TensorDataset(x, y)


class pixel_dataset(data.Dataset):
    def __init__(self, pixels):
        # pixels is ndarray of dimension (n_images, pixels_per_image, channels_per_image)
        self.pixels = torch.permute(torch.tensor(pixels), (1,0,2)) # shape (pixels_per_image, n_images, channels_per_image)

    def __len__(self):
        return self.pixels.shape[0]

    def __getitem__(self, idx):
        return self.pixels[idx]


class gain_table(nn.Module):
    def __init__(self, num_images: int, exposures=None, fixed_exposures=False):
        super(gain_table, self).__init__()
        # Store one gain value per image.
        self.table = nn.Embedding(num_images, 1)
        # let the table learn the different exposures of the images from a neutral starting point
        self.table.weight.data.fill_(0.0)
        self.num_images = num_images
        if exposures is not None:
            self.table.weight.data = exposures
            self.table.requires_grad_(not fixed_exposures)

    def forward(self, x, neutral_idx):
        # check that x is not equal to the frozen one, look up the others in the table.
        if type(neutral_idx) != torch.Tensor:
            neutral_idx = torch.tensor(neutral_idx)
        neutral_gain = self.table(neutral_idx)
        table_result = self.table(x) - neutral_gain # (n, 1)
        return torch.pow(2.0, table_result)

    def forward_matrix(self):
        return torch.cat([self.forward(torch.arange(0, self.num_images), neutral) for neutral in torch.arange(0, self.num_images)], axis=1)

    def get_gains(self, neutral_idx):
        return torch.log2(self.forward(torch.arange(0, self.num_images), neutral_idx)).detach().numpy()


class exp_function_simplified(nn.Module):
    def __init__(self, parameters: exp_parameters_simplified):
        super(exp_function_simplified, self).__init__()
        self.base = nn.parameter.Parameter(torch.log10(torch.tensor(parameters.base)))
        self.offset = nn.parameter.Parameter(torch.tensor(parameters.offset))
        self.scale = nn.parameter.Parameter(torch.tensor(parameters.scale))
        self.slope = nn.parameter.Parameter(torch.tensor(parameters.slope))
        self.intercept = nn.parameter.Parameter(torch.tensor(parameters.intercept))
        self.cut = nn.parameter.Parameter(torch.tensor(parameters.cut))

    def compute_intermediate_values(self):
        base = torch.pow(10.0, self.base)
        offset = self.offset
        scale = self.scale
        intercept = self.intercept
        cut = self.cut
        # slope = torch.abs(scale * torch.pow(base, cut) + offset - intercept) / torch.abs(cut)
        slope = (scale * torch.pow(base, cut) + offset - intercept) / torch.abs(cut)

        # self.cut = nn.parameter.Parameter(cut, requires_grad=False)
        return base, offset, scale, slope, intercept, cut

    def forward(self, t):
        base, offset, scale, slope, intercept, cut = self.compute_intermediate_values()
        interp = (t > cut).float()
        pow_value = scale * torch.pow(base, t) + offset
        lin_value = slope * t + intercept
        output = interp * pow_value + (1 - interp) * lin_value
        # output = torch.clamp(output, min=1e-6)
        return output

    def reverse(self, y):
        base, offset, scale, slope, intercept, cut = self.compute_intermediate_values()
        y_cut = slope * cut + intercept
        interp = (y > y_cut).float()
        log_value = torch.log(torch.clamp((y - offset) / scale, min=1e-7)) / torch.log(base)
        lin_value = (y - intercept) / slope
        output = interp * log_value + (1 - interp) * lin_value
        # output = torch.clamp(output, 1e-6, 1.0)
        output = torch.clamp(output, 0.0, 1.0)
        return output

    def get_log_parameters(self) -> exp_parameters_simplified:
        base, offset, scale, slope, intercept, cut = self.compute_intermediate_values()
        return exp_parameters_simplified(
            base = float(base),
            offset = float(offset),
            scale = float(scale),
            slope = float(slope),
            intercept = float(intercept),
            cut = float(cut),
        )

def plot_log_curve(model: exp_function_simplified):
    # Plot the log curve as a sanity check
    x_values = np.linspace(start=-8, stop=8, num=4000)
    x_values_lin = 0.18 * (2**x_values)
    with torch.no_grad():
        y_values = model.reverse(torch.tensor(x_values_lin)).detach().numpy()
        cut = model.get_log_parameters().cut
    plt.plot(x_values[y_values < cut], y_values[y_values < cut], color='red')
    plt.plot(x_values[y_values >= cut], y_values[y_values >= cut], color='blue')
    plt.axvline(x=0.0)
    plt.axhline(y=0.0)
    plt.title("Log curve")
    plt.show()

    plt.plot(x_values_lin[y_values < cut], y_values[y_values < cut], color='red')
    plt.plot(x_values_lin[y_values >= cut], y_values[y_values >= cut], color='blue')
    plt.axvline(x=0.18, alpha=0.5)
    plt.axvline(x=0.0)
    plt.axhline(y=0.0)
    plt.title("lin/log curve")
    plt.show()


def percent_error_masked(y, sample_mask):
    # y is of shape (batch_size, n_images, 3), need to compare the pixel values for all images in this set.
    num_valid_entries = torch.sum(sample_mask, axis=1, keepdim=True) # now shape (batch_size, 1, 3)
    y_target = y
    y_target[~sample_mask] = 0.0

    # Compute mean among not-clipped pixels for this x,y coordinate.
    target_pixel_value = torch.sum(y_target, axis=1, keepdim=True) / num_valid_entries # shape (batch_size, 1, 3)
    delta = torch.abs(y - target_pixel_value) / target_pixel_value
    return torch.mean(delta[sample_mask])


def err_fn(y, sample_mask):
    # y is of shape (batch_size, n_images, 3), need to compare the pixel values for all images in this set.
    num_valid_entries = torch.sum(sample_mask, axis=1, keepdim=True) # now shape (batch_size, 1, 3)
    y_target = y
    y_target[~sample_mask] = 0.0

    # Compute mean among not-clipped pixels for this x,y coordinate.
    target_pixel_value = torch.sum(y_target, axis=1, keepdim=True) / num_valid_entries # shape (batch_size, 1, 3)
    delta = torch.abs(torch.log(y_target+1) - torch.log(target_pixel_value+1))
    return torch.mean(delta[sample_mask])

def percent_error_target(y, target_idx, sample_mask):
    y_target = y[:, [target_idx], :] # shape (batch_size, 1, 3)
    answer_mask = torch.ones_like(y, dtype=bool)
    answer_mask[:, [target_idx], :] = 0
    sample_mask = sample_mask & answer_mask
    delta = torch.abs(y - y_target) / y_target
    return torch.mean(delta[sample_mask])

def error_fn_2(y, target_idx, sample_mask):
    y = torch.log(torch.maximum(y, torch.tensor(1e-8)))
    y_target = y[:, [target_idx], :] # shape (batch_size, 1, 3)

    answer_mask = torch.ones_like(y, dtype=bool)
    answer_mask[:, [target_idx], :] = 0
    sample_mask = sample_mask & answer_mask
    delta = torch.abs(y - y_target)
    return torch.mean(delta[sample_mask])

def reconstruction_error(y, y_original, sample_mask):
    # Assume y of shape (batch_size, n_images, n_images, 3)
    # assume y_original of shape (batch_size, 1, n_images, 3)
    # assume target_idx points to the one with gain 1.0
    # Assume y and y_original are both the log encoded images, just take L2 or L1 error.
    sample_mask = sample_mask & ~torch.isnan(y)
    # delta = torch.abs(y - y_original)
    delta = (y - y_original)**2
    return torch.mean(delta[sample_mask])

def log_lin_image_error(lin_gt, lin_pred):
    return torch.mean(torch.abs(lin_pred - lin_gt))

def negative_linear_values_penalty(y_linear):
    sample_mask = ~torch.isnan(y_linear)
    negative = torch.minimum(y_linear, torch.zeros_like(y_linear)) # zero out positive values.
    loss = negative**2
    return torch.sum(loss[sample_mask]) / torch.numel(y_linear)

def low_cut_penalty(black_point, model: exp_function_simplified):
    _, _, _, _, _, model_cut = model.compute_intermediate_values()
    return torch.abs(torch.minimum(model_cut-black_point, torch.zeros_like(black_point)))

def high_intercept_penalty(model: exp_function_simplified):
    _, _, _, _, intercept, _ = model.compute_intermediate_values()
    return torch.maximum(intercept, torch.zeros_like(intercept))**2

def smoothness_penalty(model: exp_function_simplified):
    base, offset, scale, slope, intercept, cut = model.compute_intermediate_values()
    # y_cut = slope * cut + intercept
    # log_slope = slope / scale * 1.0 / (torch.clamp((y_cut - offset) / scale, min=1e-7) * torch.log(base))
    # return torch.abs(log_slope - (1.0/slope))
    # pow_slope = scale * torch.exp(torch.log(base) * cut)
    pow_slope = scale * torch.pow(base, cut) * torch.log(base)
    return (slope - pow_slope)**2

def negative_black_point_penalty(black_lin):
    loss = torch.minimum(black_lin, torch.zeros_like(black_lin))**2 # zero if black_lin > 0
    return loss

def middle_gray_penalty(lin_img):
    # Given "properly exposed" linear image, penalize if it doesn't average at middle gray
    pos_mask = (lin_img > 1e-6) & (~torch.isnan(torch.log2(lin_img)))
    return (torch.mean(torch.log2(lin_img)[pos_mask]) - torch.log2(torch.tensor(0.18)))**2

def derive_exp_function_gd_lut(lut: lut_1d_properties, epochs: int = 100, lr=1e-3, use_scheduler=True, initial_parameters_fn=None) -> nn.Module:
    # torch.autograd.set_detect_anomaly(True)
    initial_parameters = INITIAL_GUESS if initial_parameters_fn is None else exp_parameters_simplified.from_csv(initial_parameters_fn)
    model = exp_function_simplified(initial_parameters)
    dl = data.DataLoader(dataset_from_1d_lut(lut), batch_size=lut.size)
    loss_fn = nn.L1Loss(reduction='mean')
    optim = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.MultiplicativeLR(optim, lambda x: (0.5 if x % 3000 == 0 else 1.0))
    errors = []
    losses = []
    model.train()
    with tqdm(total=epochs) as bar:
        for e in range(epochs):
            for x, y in dl:
                optim.zero_grad()
                y_pred = model(x)
                loss = torch.log(loss_fn(torch.log(y_pred + 0.5), torch.log(y + 0.5)))
                loss += 0.1 * smoothness_penalty(model)

                error = loss_fn(y, y_pred).detach()
                loss.backward()
                optim.step()
            if use_scheduler:
                sched.step()

            if e % 10 == 0:
                bar.update(10)
                bar.set_postfix(loss=float(loss), error=float(error), params=model.get_log_parameters(), lr=sched.get_last_lr())
                errors.append(float(error))
                losses.append(float(loss))


    plt.figure()
    plt.xlim(0, epochs)
    plt.ylim(min(errors[-1], losses[-1]), max(errors[0], losses[0]))
    plt.plot(range(0, epochs, 10), errors)
    plt.plot(range(0, epochs, 10), losses)
    plt.show()

    return model


def derive_exp_function_gd_log_lin_images(
    log_image: np.ndarray,
    lin_image: np.ndarray,
    black_point: float,
    epochs: int=20,
    lr=1e-3,
    use_scheduler=True,
    initial_parameters_fn=None,
    batch_size=10000,
) -> nn.Module:
    initial_parameters = INITIAL_GUESS if initial_parameters_fn is None else exp_parameters_simplified.from_csv(initial_parameters_fn)
    model = exp_function_simplified(initial_parameters)
    assert (log_image.shape == lin_image.shape) and (len(log_image.shape) == 2)
    ds = data.TensorDataset(torch.tensor(log_image), torch.tensor(lin_image))
    dl = data.DataLoader(ds, shuffle=True, batch_size=batch_size)

    optim = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.MultiplicativeLR(optim, lambda x: (0.5 if x % 20 == 0 else 1.0))
    errors = []
    losses = []
    model.train()
    for e in range(epochs):
        print(f"Training epoch {e}")
        with tqdm(total=len(ds)) as bar:
            for batch_num, (log_pixels, lin_pixels) in enumerate(dl):
                # log_pixels and lin_pixels of shape (batch_size, 3)
                batch_size = log_pixels.shape[0]

                optim.zero_grad()
                lin_pred = model(log_pixels)
                log_pred = model.reverse(lin_pixels)
                lin_black = model(black_point)

                # Ideally all of y_pred would be equal
                error = log_lin_image_error(lin_pixels, lin_pred) + log_lin_image_error(log_pixels, log_pred)
                loss = torch.log10(error)
                # loss += negative_linear_values_penalty(y_pred)
                # loss += 0.1 * negative_black_point_penalty(lin_black)
                # loss += 0.1 * low_cut_penalty(torch.tensor(black_point), model)
                loss += 0.1 * smoothness_penalty(model)

                loss.backward()
                optim.step()

                bar.update(batch_size)
                bar.set_postfix(loss=float(loss), error=float(error), params=model.get_log_parameters(), lr=sched.get_last_lr())
                errors.append(float(error))
                losses.append(float(loss))

            if use_scheduler:
                sched.step()

    plt.figure()
    plt.xlim(0, len(errors))
    plt.plot(range(len(errors)), errors)
    plt.plot(range(len(losses)), losses)
    plt.show()
    return model

def derive_exp_function_gd(
    images: np.ndarray,
    ref_image_num: int,
    white_point: float,
    black_point: float,
    epochs: int = 20,
    lr=1e-3,
    use_scheduler=True,
    exposures=None,
    fixed_exposures=False,
    initial_parameters_fn=None,
    batch_size=10000,
) -> Tuple[nn.Module, nn.Module]:
    n_images = images.shape[0]

    # torch.autograd.set_detect_anomaly(True)
    initial_parameters = INITIAL_GUESS if initial_parameters_fn is None else exp_parameters_simplified.from_csv(initial_parameters_fn)
    model = exp_function_simplified(initial_parameters)
    gains = gain_table(n_images, exposures, fixed_exposures)
    ds = pixel_dataset(images)
    dl = data.DataLoader(ds, shuffle=True, batch_size=batch_size)

    optim = torch.optim.Adam(list(model.parameters()) + list(gains.parameters()), lr=lr)
    sched = torch.optim.lr_scheduler.MultiplicativeLR(optim, lambda x: (0.5 if x % 20 == 0 else 1.0))
    errors = []
    losses = []
    model.train()
    for e in range(epochs):
        print(f"Training epoch {e}")
        with tqdm(total=len(ds)) as bar:
            for batch_num, pixels in enumerate(dl):
                # pixels is of shape (batch_size, n_images, n_channels=3), each l0 row has a fixed h,w pixel coordinate.
                batch_size = pixels.shape[0]

                optim.zero_grad()
                lin_images = model(pixels)
                lin_black = model(black_point)

                # All lin images matched to image `ref_image_num` is lin_images_matrix[:, :, ref_image_num, :]
                # lin_images_matrix[:, a, b, :] represents the transformation of image (a) when converted to the exposure of (b).
                lin_images_matrix = torch.stack([lin_images * gains(torch.arange(0, n_images), neutral_idx) for neutral_idx in torch.arange(0, n_images)], axis=2) # shape (batch_size, n_images, n_images, n_channels)
                y_pred = lin_images_matrix
                reconstructed_image = model.reverse(y_pred)

                # Ideally all of y_pred would be equal
                error = reconstruction_error(reconstructed_image, pixels[:, :, :].unsqueeze(1), sample_mask=(reconstructed_image < white_point) & (reconstructed_image > black_point) & (y_pred > 0.0) & (pixels.unsqueeze(1) < white_point))
                loss = torch.tensor(0.0)
                loss += error
                loss += 0.1 * negative_linear_values_penalty(y_pred)
                # loss += 0.1 * negative_black_point_penalty(lin_black)
                # loss += 0.1 * middle_gray_penalty(y_pred[:, ref_image_num, ref_image_num, :]) # global exposure adjustment
                loss += 0.1 * low_cut_penalty(torch.tensor(black_point), model)
                loss += 0.1 * high_intercept_penalty(model)
                loss += 0.01 * smoothness_penalty(model)
                # loss = torch.log10(loss)

                loss.backward()
                optim.step()

                bar.update(batch_size)
                bar.set_postfix(loss=float(loss), error=float(error), params=model.get_log_parameters(), lr=sched.get_last_lr())
                errors.append(float(error))
                losses.append(float(loss))

            if use_scheduler:
                sched.step()

    plt.figure()
    plt.xlim(0, len(errors))
    plt.plot(range(len(errors)), errors)
    plt.plot(range(len(losses)), losses)
    plt.show()

    return gains, model


def plot_data(x, y):
    plt.xlim(0, 1)
    plt.ylim(0, 1)
    plt.plot(x, y)
