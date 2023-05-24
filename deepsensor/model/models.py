from deepsensor import backend
from deepsensor.model.defaults import gen_encoder_scales, gen_ppu
from deepsensor.data.loader import TaskLoader
from deepsensor.data.task import Task
from deepsensor.model.nps import (
    convert_task_to_nps_args,
    run_nps_model,
    construct_neural_process,
)

import copy

from typing import List, Union

import time
from tqdm import tqdm

import numpy as np
import pandas as pd
import xarray as xr
import lab as B
from matrix import Diagonal
from plum import dispatch

# For dispatching with TF and PyTorch model types when they have not yet been loaded.
# See https://beartype.github.io/plum/types.html#moduletype
from plum import ModuleType
TFModel = ModuleType("tensorflow.keras", "Model")
TorchModel = ModuleType("torch.nn", "Module")


def create_empty_downscale_array(
    dates,
    downscaling_factor,
    reference_grid,
    coord_names={"x1": "x1", "x2": "x2"},
    data_vars=["mean", "std"],
):
    x1_lowres = reference_grid.coords[coord_names["x1"]]
    x2_lowres = reference_grid.coords[coord_names["x2"]]

    x1_hires = np.linspace(
        x1_lowres[0],
        x1_lowres[-1],
        int(x1_lowres.size * downscaling_factor),
        dtype="float32",
    )
    x2_hires = np.linspace(
        x2_lowres[0],
        x2_lowres[-1],
        int(x2_lowres.size * downscaling_factor),
        dtype="float32",
    )

    dims = ["time", coord_names["x1"], coord_names["x2"]]
    coords = {
        "time": pd.to_datetime(dates),
        coord_names["x1"]: x1_hires,
        coord_names["x2"]: x2_hires,
    }

    pred_ds = xr.Dataset(
        {data_var: xr.DataArray(dims=dims, coords=coords) for data_var in data_vars}
    )

    return pred_ds


class ProbabilisticModel:

    """
    Base class for probabilistic model used for DeepSensor.
    Ensures a set of methods required for DeepSensor
    are implemented by specific model classes that inherit from it.
    """

    def __init__(self):
        pass

    def predict(self, dataset, *args, **kwargs):
        """
        Computes the model mean prediction over target points based on given context
        data.
        """
        raise NotImplementedError()

    def covariance(self, dataset, *args, **kwargs):
        """
        Computes the model covariance matrix over target points based on given context
        data. Shape (N, N).
        """
        raise NotImplementedError()

    def variance(self, dataset, *args, **kwargs):
        """
        Model marginal variance over target points given context points.
        Shape (N,).
        """
        raise NotImplementedError()

    def stddev(self, dataset):
        """
        Model marginal standard deviation over target points given context points.
        Shape (N,).
        """
        var = self.variance(dataset)
        return var**0.5

    def entropy(self, dataset, *args, **kwargs):
        """
        Computes the model entropy over target points based on given context
        data.
        """
        raise NotImplementedError()

    def logpdf(self, dataset, *args, **kwargs):
        """
        Computes the joint model logpdf over target points based on given context
        data.
        """
        raise NotImplementedError()

    def loss(self, dataset, *args, **kwargs):
        """
        Computes the model loss over target points based on given context data.
        """
        raise NotImplementedError()

    def sample(self, dataset, n_samples=1, *args, **kwargs):
        """
        Draws `n_samples` joint samples over target points based on given context
        data.
        returned shape is (n_samples, n_target).
        """
        raise NotImplementedError()

    def mutual_information(
        self, dataset, X_new, Y_new, *args, context_set_idx=0, **kwargs
    ):
        """
        WIP: Old code not using new dataset data structure.

        Computes the mutual information over target set T given context set C and
        the new (proposed) context set N:
            I(T|C;N) = H(T|C) - H(T|C,N)

        Uses the subclassed `entropy` method.
        """

        dataset_with_new = concat_obs_to_dataset(dataset, X_new, Y_new, context_set_idx)

        entropy_before = self.entropy(dataset)

        entropy_after = self.entropy(dataset_with_new)

        return entropy_before - entropy_after


class DeepSensorModel(ProbabilisticModel):

    """
    Implements DeepSensor prediction functionality of a ProbabilisticModel.
    Allows for outputting an xarray object containing on-grid predictions or a pandas
    object containing off-grid predictions.
    """

    def predict_ongrid(
        self,
        tasks: Union[List[dict], dict],
        reference_grid: xr.Dataset,
        downscaling_factor=1,
        n_samples=0,
        seed=0,
        progress_bar=0,
        verbose=False,
    ):
        """Predict on a regular grid

        Returns:
            pred_ds (xr.Dataset): Dataset containing the predictions
        """
        tic = time.time()

        if type(tasks) is Task:
            tasks = [tasks]

        vars = ["mean", "std"]
        if n_samples >= 1:
            vars.extend(list(range(n_samples)))

        dates = [task["time"] for task in tasks]
        pred_ds = create_empty_downscale_array(
            dates, downscaling_factor, reference_grid, data_vars=vars
        )

        X_target = [
            (pred_ds["x1"].values, pred_ds["x2"].values) for i in range(1)
        ]  # TODO - fix this by using number of targets

        for task in tqdm(tasks, position=0, disable=progress_bar < 1, leave=True):
            predictions = {var: [] for var in vars}

            task["X_t"] = X_target

            # Run model forwards once to generate output distribution
            dist = self(task, n_samples=n_samples)

            predictions["mean"].append(self.predict(dist))
            predictions["std"].append(self.stddev(dist))
            if n_samples >= 1:
                # Is there a way to jointly draw N batched samples?
                B.set_random_seed(seed)
                np.random.seed(seed)
                samples = self.sample(dist, n_samples=n_samples)
                for i, sample in enumerate(samples):
                    # This could be improved
                    predictions[i].append(sample)

            # Convert batched outputs to single arrays
            for var in vars:
                arr = np.array(predictions[var])
                # Store
                pred_ds[var].loc[task["time"], :, :] = arr[
                    0
                ]  # Slice 1st batch dim what about multiple targets? TODO fix

        if n_samples >= 1:
            mean_std_pred_ds = pred_ds[["mean", "std"]]
            pred_ds = pred_ds.drop(["mean", "std"])
            sample_pred_ds = pred_ds.to_array().rename({"variable": "sample"})
            prediction_ds_dict = {"samples": sample_pred_ds}
            prediction_ds_dict["mean"] = mean_std_pred_ds["mean"]
            prediction_ds_dict["std"] = mean_std_pred_ds["std"]
            pred_ds = xr.Dataset(prediction_ds_dict)

        # Convert time coord to pandas timestamps
        pred_ds = pred_ds.assign_coords(time=pd.to_datetime(pred_ds.time.values))

        if verbose:
            dur = time.time() - tic
            print(f"Done in {np.floor(dur / 60)}m:{dur % 60:.0f}s.\n")

        return pred_ds


class ConvNP(DeepSensorModel):

    """
    Implements a ConvNP regression probabilistic model.

    Multiple dispatch is implemented using `plum` to allow for re-using
    the model's forward prediction object when computing the logpdf, entropy, etc.
    Alternatively, the model can be run forwards with a `task` dictionary of data
    from the `DataLoader`.

    TODO put dim_y dimension back in to support N-D predictions.
    """

    @dispatch
    def __init__(self, *args, **kwargs):
        """Generate a new model with default or specified parameters"""
        self.model = construct_neural_process(
            *args,
            **kwargs,
        )

    @dispatch
    def __init__(self, task_loader: TaskLoader, *args, **kwargs):
        """Generate a new model from TaskLoader, using data to infer sensible model parameters

        Args:
            task_loader (TaskLoader): TaskLoader object
        """
        if "dim_yc" not in kwargs:
            dim_yc = task_loader.context_dims
            print(f"dim_yc inferred from TaskLoader: {dim_yc}")
            kwargs["dim_yc"] = dim_yc
        if "dim_yt" not in kwargs:
            dim_yt = sum(task_loader.target_dims)  # Must be an int
            print(f"dim_yt inferred from TaskLoader: {dim_yt}")
            kwargs["dim_yt"] = dim_yt
        if "points_per_unit" not in kwargs:
            ppu = gen_ppu(task_loader)
            print(f"points_per_unit inferred from TaskLoader: {ppu}")
            kwargs["points_per_unit"] = ppu
        if "encoder_scales" not in kwargs:
            encoder_scales = gen_encoder_scales(kwargs["points_per_unit"], task_loader)
            print(f"encoder_scales inferred from TaskLoader: {encoder_scales}")
            kwargs["encoder_scales"] = encoder_scales

        self.model = construct_neural_process(
            dim_x=2,
            *args,
            **kwargs,
        )

    @dispatch
    def __init__(self, neural_process: Union[TFModel, TorchModel]):
        """Use existing model object."""
        self.model = neural_process

    @classmethod
    def modify_task(cls, task):
        """Cast numpy arrays to TensorFlow or PyTorch tensors, add batch dim, and mask NaNs"""

        def array_modify_fn(arr):
            arr = arr[np.newaxis, ...]  # Add batch dim

            # Find NaNs and keep size-1 variable dim
            mask = np.any(np.isnan(arr), axis=1, keepdims=False)
            if np.any(mask):
                # Set NaNs to zero - necessary for `neural_process`
                arr[mask] = 0.0

            arr = backend.convert_to_tensor(arr)

            # Convert to `nps.Masked` object if there are NaNs
            if B.any(mask):
                arr = backend.nps.Masked(arr, B.cast(B.dtype(arr), mask))

            return arr

        task = task.modify(array_modify_fn, modify_flag="NPS")
        return task

    @classmethod
    def check_task(cls, task):
        """Check that the task is compatible with the model."""
        if task["modify"] is None:
            task = cls.modify_task(task)
        elif task["modify"] != "NPS":
            raise ValueError(f"Task has been modified for {task['modify']}.")
        return task

    def __call__(self, task, n_samples=10):
        """Compute ConvNP distribution."""
        task = ConvNP.check_task(task)
        dist = run_nps_model(self.model, task, n_samples)
        return dist

    @dispatch
    def predict(self, dist: backend.nps.AbstractMultiOutputDistribution):
        return dist.mean.numpy()[0, 0, 0]

    @dispatch
    def predict(self, task: dict):
        return self(task).mean.numpy()[0, 0, 0]

    @dispatch
    def entropy(self, dist: backend.nps.AbstractMultiOutputDistribution):
        """Model entropy over target points given context points."""
        return dist.entropy().numpy()[0, 0]

    @dispatch
    def entropy(self, task: dict):
        return self(task).entropy().numpy()[0, 0]

    @dispatch
    def covariance(self, dist: backend.nps.AbstractMultiOutputDistribution):
        return B.dense(dist.vectorised_normal.var).numpy()[0, 0]

    @dispatch
    def covariance(self, task: dict):
        return B.dense(self(task).vectorised_normal.var).numpy()[0, 0]

    @dispatch
    def variance(self, dist: backend.nps.AbstractMultiOutputDistribution):
        return dist.var.numpy()[0, 0, 0]

    @dispatch
    def variance(self, task: dict):
        return self(task).var.numpy()[0, 0, 0]

    @dispatch
    def logpdf(
        self, dist: backend.nps.AbstractMultiOutputDistribution, task: dict, station_set_idx=0
    ):
        # Need Y_target to be the right shape for model in the event that task is from the
        # default DataLoader... is this the best way to do this?
        task = ConvNP.check_task(task)

        Y_target = task["Y_t"][station_set_idx]
        return dist.logpdf(Y_target).numpy().mean()

    @dispatch
    def logpdf(self, task: dict, station_set_idx=0):
        # Need Y_target to be the right shape for model in the event that task is from the
        # default DataLoader... is this the best way to do this?
        task = ConvNP.check_task(task)

        Y_target = task["Y_t"][station_set_idx]
        return self(task).logpdf(Y_target).numpy().mean()

    def loss_fn(self, task, fix_noise=None, num_lv_samples=8, normalise=False):
        """

        Parameters
        ----------
        model_config
        neural_process
        task
        num_lv_samples: If latent variable model, number of lv samples for evaluating the loss
        normalise

        Returns
        -------

        """
        task = ConvNP.check_task(task)
        context_data, xt, yt, model_kwargs = convert_task_to_nps_args(task)

        logpdfs = backend.nps.loglik(
            self.model,
            context_data,
            xt,
            yt,
            **model_kwargs,
            fix_noise=fix_noise,
            num_samples=num_lv_samples,
            normalise=normalise,
        )

        loss = -B.mean(logpdfs)

        return loss

    @dispatch
    # TEMP trying wessel's `num_samples` kwarg for model call signature, followed by simple .sample() call
    def sample(self, dist: backend.nps.AbstractMultiOutputDistribution, n_samples=1):
        return dist.noiseless.sample(n_samples).numpy()[
            :, 0, 0, 0
        ]  # first batch, first feature

    @dispatch
    def sample(self, task: dict, n_samples=1):
        return (
            self(task).noiseless.sample(n_samples).numpy()[:, 0, 0, 0]
        )  # first batch, first feature

    @dispatch
    def slice_diag(self, task: dict):
        """Slice out the ConvCNP part of the ConvNP distribution."""
        dist = self(task)
        dist_diag = backend.nps.MultiOutputNormal(
            dist._mean,
            B.zeros(dist._var),
            Diagonal(B.diag(dist._noise + dist._var)),
            dist.shape,
        )
        return dist_diag

    @dispatch
    def slice_diag(self, dist: backend.nps.AbstractMultiOutputDistribution):
        """Slice out the ConvCNP part of the ConvNP distribution."""
        dist_diag = backend.nps.MultiOutputNormal(
            dist._mean,
            B.zeros(dist._var),
            Diagonal(B.diag(dist._noise + dist._var)),
            dist.shape,
        )
        return dist_diag

    def ar_sample(
        self, task: dict, n_samples=1, X_target_AR=None, ar_subsample_factor=1
    ):
        """AR sampling with optional functionality to only draw AR samples over a subset of the
        target set and then infull the rest of the sample with the model mean conditioned on the
        AR samples."""
        if X_target_AR is not None:
            # User has specified a set of locations to draw AR samples over
            task_arsample = copy.deepcopy(task)
            task_arsample["X_t"][0] = X_target_AR
        elif ar_subsample_factor > 1:
            # Subsample target locations to draw AR samples over
            task_arsample = copy.deepcopy(task)
            xt = task["X_t"][0]
            ndim_2d = np.sqrt(xt.shape[-1])
            if int(ndim_2d) == ndim_2d:
                # Targets on a square: subsample targets for AR along both spatial dimension
                xt_2d = xt.reshape(-1, int(ndim_2d), int(ndim_2d))
                xt = xt_2d[..., ::ar_subsample_factor, ::ar_subsample_factor].reshape(
                    2, -1
                )
            else:
                xt = xt[..., ::ar_subsample_factor]
            task_arsample["X_t"][0] = xt
        else:
            task_arsample = copy.deepcopy(task)

        task_arsample = ConvNP.check_task(task_arsample)
        task = ConvNP.check_task(task)

        mean, variance, noiseless_samples, noisy_samples = run_nps_model_ar(
            self.model, task_arsample, num_samples=n_samples
        )

        # Slice out first (and assumed only) target entry in nps.Aggregate object
        noiseless_samples = noiseless_samples[0].numpy()

        if ar_subsample_factor > 1 or X_target_AR is not None:
            # AR sample locations not equal to target locations - infill the rest of the
            # sample with the model mean conditioned on the AR samples
            full_samples = []
            for sample in noiseless_samples:
                task_with_sample = copy.deepcopy(task)
                task_with_sample["X_c"][0] = np.concatenate(
                    [task["X_c"][0], task_arsample["X_t"][0]], axis=-1
                )
                task_with_sample["Y_c"][0] = np.concatenate(
                    [task["Y_c"][0], sample], axis=-1
                )

                if type(self) is ConvCNP:
                    # Independent predictions for each target location - just compute the mean
                    # conditioned on the AR samples
                    pred = self.predict(
                        task_with_sample
                    )  # Should this be a `.sample` call?
                else:
                    # Sample from joint distribution over all target locations
                    # NOTE Risky to assume all model classes other than `ConvCNP` model correlations.
                    pred = self.sample(task_with_sample, n_samples=1)

                full_samples.append(pred)
            full_samples = np.stack(full_samples, axis=0)

            return full_samples
        else:
            return noiseless_samples[
                :, 0, 0
            ]  # Slice out batch dim and feature dim (assumed size 1)